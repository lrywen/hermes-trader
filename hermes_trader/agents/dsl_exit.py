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
import shutil
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

from hermes_trader.agents import atomic_io
from hermes_trader.agents.config_store import cfg_get
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

# ── In-process write throttle ─────────────────────────────────────────
# The trading loop calls check() on every WS mid tick (often several per
# second per position). Saving the full registry on every floor/peak tick
# thrashes the lock file and contends with dashboard readers. Peak is a
# ratchet that rebuilds from marks after a restart (the floor itself is
# what matters and is monotonic-clamped), so peak-only changes don't need
# to hit disk. Floor moves and exit verdicts request a save; the request
# is coalesced to at most once per this interval process-wide, with a
# dirty flag ensuring a deferred floor move is flushed on the next tick.
# Force-saves (register/deregister/exit verdict) bypass the throttle.
# R13-B1: read from the canonical dsl_state_io block; legacy HERMES_DSL_*
# env var still wins (operator override preserved for backward compat).
_MIN_SAVE_INTERVAL_SEC = float(
    os.environ.get("HERMES_DSL_SAVE_INTERVAL_SEC")
    or cfg_get("dsl_state_io.save_min_interval_sec", config={})
)
_LAST_SAVE_TS: float = 0.0
_SAVE_DIRTY: bool = False

# Throttle for force-reloads from read-only consumers (dashboard). Each
# dashboard poll used to clear() + re-read the state file under LOCK_SH,
# contending with the trading loop's LOCK_EX writes on every HTTP request.
# The DSL floor only changes on check() ticks (throttled above to ~2s), so
# a force-reload fresher than this TTL returns the in-memory copy without
# touching the lock file. Default 1s keeps the operator view near-live.
# R13-B1: canonical-block read; legacy env still wins.
_FORCE_LOAD_TTL_S = float(
    os.environ.get("HERMES_DSL_FORCE_LOAD_TTL_S")
    or cfg_get("dsl_state_io.force_load_ttl_s", config={})
)
_LAST_FORCE_LOAD_TS: float = 0.0

# TTL cache for the config-derived ExitPolicy. Rehydrate can synthesize many
# trackers per scan; without this each one re-read and re-parsed the config
# file. Config edits are operator-driven, so a short TTL (default 5s) keeps
# newly applied stops near-immediate without per-tracker disk I/O.
# R13-B1: canonical-block read; legacy env still wins.
_POLICY_CACHE_TTL_S = float(
    os.environ.get("HERMES_DSL_POLICY_CACHE_TTL_S")
    or cfg_get("dsl_state_io.policy_cache_ttl_s", config={})
)
_POLICY_CACHE: Optional["ExitPolicy"] = None
_POLICY_CACHE_TS: float = 0.0


# ── Prometheus metric emission (best-effort, never raises) ────────────
# Metrics are imported lazily to keep dsl_exit importable in isolation and
# to avoid any import-time coupling. All emission is guarded so a missing
# or broken metrics module can never break the trade hot path.
def _record_exit(reason: str) -> None:
    """Increment hermes_dsl_exits_total, labelled by the base exit reason.

    The reason string carries a human-readable suffix (e.g.
    "max_loss (2.50% spot ...)"); label on the stable token before the first
    separator so cardinality stays bounded. Unknown shapes land under
    "other" rather than exploding the label space.
    """
    try:
        from hermes_trader.metrics import DSL_EXITS
        known = {
            "max_loss", "floor_breach", "hard_timeout", "stale_flat_timeout",
            "time_scratch",  # Audit 2026-09-06 (E3, P2)
        }
        label = "other"
        if reason:
            token = reason.split(" ", 1)[0].split("(", 1)[0]
            if token in known:
                label = token
        DSL_EXITS.labels(reason=label).inc()
    except Exception:
        pass


def _record_floor_move() -> None:
    """Increment hermes_dsl_floor_moves_total on a real monotonic ratchet step."""
    try:
        from hermes_trader.metrics import DSL_FLOOR_MOVES
        DSL_FLOOR_MOVES.inc()
    except Exception:
        pass


def _record_stop_tuning_shadow(tracker: "DSLTracker", pol: "ExitPolicy",
                               effective_max_loss: float, loss_pct: float,
                               roe_loss: float, atr_active: bool,
                               spot_cap_display: float) -> None:
    """Shadow (3): record whether a wider cap / lower breakeven would matter.

    Audit 2026-09-10 (ADA/ZEC died at the 0.80% spot cap; the 2.5% breakeven
    never armed). At a max_loss exit this appends ONE risk-tuning record with:
      * would="survive" when the adverse excursion at exit is still inside the
        candidate wider cap (i.e. the proposed stop would NOT have stopped out
        at this tick — counter-factual, not a profit claim);
      * whether the realized MFE would have armed a candidate lower breakeven.
    Live stop behaviour is unchanged. Best-effort, never raises.
    """
    try:
        from hermes_trader.agents.config_store import read_agent_config
        cfg = read_agent_config() or {}
        tun = ((cfg.get("dsl_exit") or {}).get("stop_tuning_shadow")) or {}
        if not bool(tun.get("shadow_mode", False)):
            return

        wider_cap = tun.get("candidate_max_loss_pct")
        try:
            wider_cap = float(wider_cap) if wider_cap is not None else None
        except (TypeError, ValueError):
            wider_cap = None
        cand_be = tun.get("candidate_breakeven_trigger_pct")
        try:
            cand_be = float(cand_be) if cand_be is not None else None
        except (TypeError, ValueError):
            cand_be = None

        mfe_pct = float(tracker._peak_profit_pct())
        would_survive = (
            wider_cap is not None and abs(loss_pct) < wider_cap
            and abs(loss_pct) >= effective_max_loss
        )
        be_would_arm = cand_be is not None and mfe_pct >= cand_be
        if not (would_survive or be_would_arm):
            return

        import os
        from datetime import datetime, timezone
        from hermes_trader.shadow_log import append_jsonl
        path = os.environ.get(
            "HERMES_RISK_TUNING_SHADOW_FILE",
            os.path.expanduser("~/.hermes-trading/risk_tuning_shadow.jsonl"))
        append_jsonl(path, {
            "timestamp": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "rule": "stop_tuning",
            "coin": tracker.coin,
            "side": tracker.side,
            "would": ("survive_wider_cap" if would_survive
                      else "breakeven_would_arm"),
            "detail": {
                "live_spot_cap_pct": round(float(spot_cap_display), 4),
                "loss_spot_pct": round(float(loss_pct), 4),
                "loss_roe_pct": round(float(roe_loss), 4),
                "atr_stop_active": bool(atr_active),
                "mfe_spot_pct": round(mfe_pct, 4),
                "candidate_max_loss_pct": wider_cap,
                "candidate_breakeven_trigger_pct": cand_be,
                "live_breakeven_trigger_pct": float(pol.breakeven_trigger_pct),
                "breakeven_would_have_armed": bool(be_would_arm),
                "entry_px": float(tracker.entry_px),
                "leverage": int(getattr(tracker, "leverage", 0) or 0),
            },
        }, stream="risk_tuning")
    except Exception as e:  # never break the exit hot path
        logger.debug("[dsl] stop-tuning shadow record failed: %r", e)


def _refresh_positions_gauge() -> None:
    """Set hermes_dsl_positions to the current registry size."""
    try:
        from hermes_trader.metrics import DSL_POSITIONS
        DSL_POSITIONS.set(len(_active_positions))
    except Exception:
        pass


def _resolve_fill_time_ms(
    user: str,
    coin: str,
    side: str,
    entry_px: Optional[float] = None,
    size: Optional[float] = None,
    within_minutes: float = 1440.0,
) -> Optional[float]:
    """Resolve the OPENING fill time for this position from userFills.

    Matches coin + side, then narrows by size (when provided) and price
    (when ``entry_px`` is provided), and requires the fill to be recent
    (within ``within_minutes``) so a prior round-trip on the same coin
    isn't mistaken for the current position. Returns epoch seconds or
    None on any failure / no confident match.

    The Hyperliquid ``userFills`` endpoint returns newest-first. We pull
    up to 50 (the same limit resolve_close_fill uses) because an active
    account can have >5 fills across other coins in the seconds between
    the opening fill and rehydrate.
    """
    try:
        fills = _http_post(
            "/info", {"type": "userFills", "user": user, "limit": 50}, timeout=8
        )
        if not isinstance(fills, list):
            return None
        want_long = side == "long"
        now_s = time.time()
        cutoff_s = now_s - within_minutes * 60.0
        # First pass: collect all same-coin/same-direction opening fills
        # within the recency window, newest first.
        candidates: list[dict[str, Any]] = []
        for f in fills:
            if not isinstance(f, dict):
                continue
            if f.get("coin") != coin:
                continue
            fside = f.get("side")
            f_long = (fside == "B")
            if f_long != want_long:
                continue
            try:
                ftime_s = int(f.get("time", 0)) / 1000.0
            except (TypeError, ValueError):
                continue
            if ftime_s < cutoff_s:
                # fills are newest-first; once we fall past the window the
                # rest are all older, so stop scanning.
                break
            candidates.append((ftime_s, f))
        if not candidates:
            return None
        # Narrow by size (exact match on the opening leg) when the caller
        # knows it. Hyperliquid sizes are strings; compare as float with a
        # tight tolerance to avoid 1.000 vs 0.999 float noise.
        if size is not None and size > 0:
            sized = [
                (t, f) for t, f in candidates
                if _fills_size_match(f.get("sz"), size)
            ]
            if sized:
                candidates = sized
        # Narrow by price (entryPx from the live position). Same tight
        # tolerance — a 0.01% match is plenty given tick sizes.
        if entry_px is not None and entry_px > 0:
            priced = [
                (t, f) for t, f in candidates
                if _fills_price_match(f.get("px"), entry_px)
            ]
            if priced:
                candidates = priced
        # candidates is still newest-first; the first remaining match is
        # the most recent opening fill, which is what we want.
        return candidates[0][0]
    except Exception:
        logger.debug(f"[dsl] fill-time lookup failed for {coin} {side} (non-fatal)")
    return None


def _fills_size_match(a: Any, b: float, tol: float = 1e-6) -> bool:
    """True if fill size ``a`` (str/float) matches ``b`` within ``tol``."""
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def _fills_price_match(a: Any, b: float, rel_tol: float = 1e-4) -> bool:
    """True if fill price ``a`` matches ``b`` within ``rel_tol`` (0.01%)."""
    try:
        return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def resolve_close_fill(user: str, coin: str, side: str,
                       since_ts: float) -> Optional[dict[str, Any]]:
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


def _fetch_open_orders(user: str) -> Optional[list[dict[str, Any]]]:
    """Fetch the user's resting orders from the HL REST endpoint.

    Returns a list (possibly EMPTY — an empty book is a real, actionable
    result) or None when the lookup itself failed (network/timeout/parse).
    Callers verifying HELD oids must treat None as ambiguous and defer — never
    as "every stop vanished".
    """
    try:
        data = _http_post("/info", {"type": "openOrders", "user": user}, timeout=8)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.debug(f"[dsl] openOrders lookup failed (state ambiguous): {e}")
        return None


def _order_trigger_px(o: dict[str, Any]) -> Optional[float]:
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


def _find_sl_replacement(
    orders: list[dict[str, Any]], tracker: "DSLTracker",
    mark_px: Optional[float] = None,
) -> Optional[tuple[int, float, Optional[float]]]:
    """Find the cancel+replace NEW SL order for a tracker whose held oid is gone.

    Matches THIS coin's reduce-only trigger resting on the SL side, sized within
    the same 2%/0.001 tolerance the mover clamps to. With several candidates the
    one closest to the tracker's last known sl_px wins (a hint, never a gate).
    Returns (oid, trigger_px, size) or None.

    Side boundary: once Phase-2 trailing starts the SL ratchets and can CROSS
    entry (a long's locked-in stop ABOVE entry), so classifying against entry
    would read the replacement as a TP and never claim it. When `mark_px` is
    known the side is classified against the live mark (mirrors
    exchange.find_sl_trigger_in_open_orders); without it the entry rule is the
    conservative fallback (pre-trailing / no mark derivable).
    """
    ref_px = mark_px if mark_px else tracker.entry_px
    candidates: list[tuple[float, int, float, Optional[float]]] = []
    for o in orders:
        if o.get("coin") != tracker.coin or o.get("reduceOnly") is not True:
            continue
        oid = _opt_int(o.get("oid"))
        tpx = _order_trigger_px(o)
        if oid is None or tpx is None:
            continue
        is_sl_side = (tracker.is_long() and tpx < ref_px) or \
                     (not tracker.is_long() and tpx > ref_px)
        if not is_sl_side:
            continue
        try:
            sz = abs(float(o.get("sz", 0) or 0))
        except (TypeError, ValueError):
            continue
        want = tracker.sl_size or 0.0
        if want > 0 and abs(sz - want) > max(0.001, want * 0.02):
            continue
        dist = abs(tpx - tracker.sl_px) if tracker.sl_px else 0.0
        candidates.append((dist, oid, tpx, sz))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _, oid, tpx, sz = candidates[0]
    return oid, tpx, sz


def backfill_brackets_from_exchange(
    user: Optional[str],
    marks: Optional[dict[str, float]] = None,
) -> int:
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
    # Global flag distinguishing this process's first backfill (verify every
    # held oid once) from steady-state ticks (query only when an oid is missing
    # or suspected dead). The flag is module-level so tests exercising the
    # idempotent second-call contract see exactly one verification pass.
    global _held_oids_verified
    # Only query when there is work: a missing oid, a suspected-dead oid, or
    # the one-shot startup verification of already-held oids.
    has_missing = any(t.sl_oid is None for t in _active_positions.values())
    has_suspect = any(
        f"{t.coin}_{t.side}" in _suspect_sl_keys for t in _active_positions.values()
    )
    verify_held = not _held_oids_verified
    if not (has_missing or has_suspect or verify_held):
        return 0
    orders = _fetch_open_orders(user)
    # None = lookup failed (AMBIGUOUS): an API blip must never be read as
    # "every SL vanished" — that would null live oids on the startup verify.
    # Defer ALL verification until a readable snapshot arrives. An empty list
    # is a real, readable result.
    if orders is None:
        logger.debug("[dsl] backfill: openOrders lookup failed; deferring")
        return 0
    _held_oids_verified = True
    # Index every resting oid once for cheap held-oid liveness checks.
    live_oids: set[int] = set()
    for o in orders:
        oid = _opt_int(o.get("oid"))
        if oid is not None:
            live_oids.add(oid)
    updated = 0
    marks = marks or {}
    for tracker in _active_positions.values():
        key = f"{tracker.coin}_{tracker.side}"
        is_suspect = key in _suspect_sl_keys
        # Live mark when derivable (from the caller's position snapshot). Used
        # only as the SL-vs-TP side boundary: a trailing SL can cross entry, so
        # the entry rule alone would misread a locked-in stop as a TP. Missing
        # mark falls back to entry (safe for pre-trailing / fresh synth).
        mark_px = marks.get(tracker.coin)
        # ── Held-oid liveness (startup one-shot, or suspected-dead) ──────
        # A held oid that still rests is authoritative: never replace it (this
        # preserves the backfill idempotency contract — a healthy tracker is
        # never overwritten by a spurious same-coin candidate).
        if tracker.sl_oid is not None and (verify_held or is_suspect):
            if int(tracker.sl_oid) in live_oids:
                _clear_sl_suspect(tracker)
            elif is_suspect:
                # Held oid confirmed gone: look for the cancel+replace's new
                # order among THIS coin's SL-side reduce-only triggers.
                repl = _find_sl_replacement(orders, tracker, mark_px=mark_px)
                if repl is not None:
                    oid, tpx, sz = repl
                    tracker.sl_oid = oid
                    tracker.sl_px = tpx
                    if sz:
                        tracker.sl_size = sz
                    if tracker._last_floor is None:
                        tracker._last_floor = tpx
                    _clear_sl_suspect(tracker)
                    updated += 1
                    logger.warning(
                        f"[dsl] backfill: {key} dead sl_oid replaced by "
                        f"reconciled oid={oid} trigger={tpx}"
                    )
                else:
                    # Confirmed absent and no attributable replacement. Null
                    # the dead oid so the position is visibly unprotected (the
                    # mover skips nulls rather than silently pinning a dead
                    # order) and scream for manual re-arm; DSL soft stop still
                    # monitors. Never do this on a failed lookup (handled above).
                    logger.error(
                        f"[dsl] backfill: {key} held sl_oid={tracker.sl_oid} "
                        f"CONFIRMED MISSING on exchange; no replacement found"
                    )
                    tracker.sl_oid = None
                    _clear_sl_suspect(tracker)
                    updated += 1
                    try:
                        from hermes_trader import notify
                        notify.send_text(
                            f"🚨 {tracker.coin} 交易所端止损单确认丢失（旧 oid 已失效），"
                            f"未找到可认领的替代止损。DSL 软止损仍在监控，请立即人工核对"
                            f"持仓并补挂止损单。",
                            category="risk")
                    except Exception as alert_e:
                        # H-P3: this is the naked-stop warning; a failed alert
                        # would leave an unprotected position with no signal.
                        logger.error("[dsl] backfill missing-SL risk alert failed for %s: %r",
                                     tracker.coin, alert_e)
            # On the startup-only verify path a missing held oid with no suspect
            # flag is left untouched: steady openOrders shape drift (e.g. a
            # trigger mid-transition) must not null a healthy oid on a single
            # empty-ish snapshot. Suspect entries are the executor's explicit
            # "response lost" signal and are safe to act on as above.
            # This tracker's SL was already reconciled (or deliberately left
            # untouched): never fall through to the missing-oid fill scan in
            # the same iteration, which could double-count the just-claimed
            # replacement or overwrite it with a same-coin candidate.
            continue
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
            # Classify by direction relative to the live mark when known (a
            # trailing SL can cross entry into profit; using entry there would
            # misread the stop as a TP and leave the oid unfilled), else entry:
            # a backup SL for a long is BELOW the boundary (sell trigger), a TP
            # scale-out ABOVE it; shorts are the mirror. Robust even when the
            # exchange payload omits an explicit tpsl tag.
            ref_px = mark_px if mark_px else tracker.entry_px
            is_sl_side = (tracker.is_long() and tpx < ref_px) or \
                         (not tracker.is_long() and tpx > ref_px)
            try:
                sz = abs(float(o.get("sz", 0) or 0))
            except (TypeError, ValueError):
                sz = None
            if is_sl_side and tracker.sl_oid is None:
                tracker.sl_oid = oid
                tracker.sl_px = tpx
                if sz:
                    tracker.sl_size = sz
                # Audit 2026-09-06 (E5, P2): seed a CONSERVATIVE trailing floor
                # from the resting bracket SL for synth trackers that have none
                # (state wiped / restarted). Without this, _monotonic_clamp
                # treats prev_floor=None as unconstrained and the ratchet can
                # snap back to its loosest value (the "ratchet reset to zero"
                # defect). The exchange bracket SL is the exchange-enforced
                # stop, so using its trigger as the initial floor can never
                # widen risk beyond what the bracket already guarantees, and
                # is always on the correct side of the live boundary (long:
                # sl below the mark/entry, short above, established by
                # is_sl_side above); a locked-in trailing stop may rest past
                # entry, which is still the safe ratchet direction.
                if tracker._last_floor is None:
                    tracker._last_floor = tpx
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
    phase2_tiers: list[RetraceTier] = field(default_factory=lambda: [
        RetraceTier(8.0, 0.35),   # 8% profit → give back 35%
        RetraceTier(15.0, 0.40),  # 15% profit → give back 40% (let winners run)
    ])
    consecutive_breaches_required: int = 1  # Number of consecutive floor breaches before exit
    # ── Time-based breach confirmation (clock-based, not tick-based) ────
    # When > 0, a floor breach must PERSIST for this many seconds before
    # the exit fires. A single tick poking below the floor then recovering
    # (common during wicks / WS mid flurries) resets the timer and does
    # NOT exit. This replaces the old "N consecutive ticks" gate, which
    # was tick-rate dependent: at 5 ticks/s N=3 is 0.6s, at 1 tick/3s N=3
    # is 9s — the same config meant wildly different things. The clock is
    # stable across feed rates. 0 disables the time gate (immediate exit,
    # or tick-count gate if consecutive_breaches_required > 1).
    # A-F5 (deep audit 2026-08-28): default was 0.0 — a single instantaneous
    # mid tick through the floor (a wick that the WS allMids feed briefly
    # marked) market-closed a winning position. The loop polls every ~15s,
    # so a sub-second wick was invisible to a "consecutive tick" gate but
    # one mid tick was enough to fire an exit. Default is now 4s (audit
    # recommendation 3–5s): a breach must persist across two poll cycles,
    # and exit confirmation additionally requires the INDEX price (oracle,
    # which a single-book wick cannot move) to be through the floor.
    breach_confirm_sec: float = 4.0
    # ── H-5 (supplemental audit 2026-08-30): hard max_loss stop wick guard ──
    # The trailing floor_breach below gets both a time gate (breach_confirm_sec)
    # and an INDEX (oracle) cross-check (A-F5), but the max_loss hard stop above
    # them fired on the FIRST mid tick at the cap — a single-book wick through
    # the hard stop market-closed a position even when the index never traded
    # there. The hard stop still has to be FAST (it bounds catastrophe and the
    # exchange-side bracket SL is the ultimate net), so its persistence window
    # is deliberately SHORT (default 1.0s — enough to reject a one-tick wick at
    # a ~15s poll, far inside the exchange backup-SL slippage budget) and the
    # index cross-check only SUPPRESSES while the index is healthy and on the
    # safe side. 0 disables the time gate. A missing/invalid index degrades to
    # the time gate on mid alone (fail-open; exchange backup SL remains).
    hard_stop_confirm_sec: float = 1.0
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
    # ── P2 candidate: smooth phase1→phase2 floor transition ───────────────
    # At the arm instant (peak profit crosses protect_pct) the baseline floor
    # SNAPS in one tick from the hard max-loss stop (below entry) up to the
    # full trailing floor (~entry + protect*(1-retrace), already ABOVE entry).
    # A noise wick right at that boundary can then stop the trade out before
    # the peak ever meaningfully extends — and the arm+exit can happen inside
    # a single 1m candle, invisible to OHLCV replay. When enabled, the phase-2
    # floor instead RAMPS from the hard stop to the full trailing floor across
    # a peak-profit band of width `smooth_band_pct` (progress 0 at peak==
    # protect, progress 1 at peak>=protect+band). Past the band the floor is
    # byte-identical to the baseline trail; the monotonic clamp downstream
    # still guarantees the floor never loosens. DEFAULT OFF (inert) — enabled
    # only for tick-level A/B replay (scripts/p2_smooth_replay.py), same
    # validation gate as the noise band above.
    smooth_transition_enabled: bool = False
    smooth_band_pct: float = 1.0  # peak-profit % over which the floor ramps hard→trail

    # Audit 2026-09-06 (E3, P2): time-based scratch exit. A chop-market leak:
    # a position that never arms phase-2 (peak never reached protect_pct) but
    # printed a small favorable pulse can sit until hard_timeout while that
    # pulse fades back to flat. When enabled, check() exits once the position
    # has aged past `time_scratch_minutes`, is still green (never masks a
    # loss), had peaked at least `time_scratch_min_peak_pct`, remains below
    # the protect arm threshold (phase-2 trail keeps sovereignty once armed),
    # and has given back `time_scratch_giveback_pct` from its peak. DEFAULT
    # OFF (inert); opt in via dsl_exit.time_scratch config block.
    time_scratch_enabled: bool = False
    time_scratch_minutes: float = 60.0
    time_scratch_min_peak_pct: float = 0.3
    time_scratch_giveback_pct: float = 0.3


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
        # monotonic() timestamp of the first tick in the current sustained
        # breach run; None while not breached or after recovery. Used by the
        # time-based breach_confirm_sec gate (P2-7). Not persisted: on
        # restart it just re-arms from the first post-restart breach tick.
        self._first_breach_ts: Optional[float] = None
        self._last_floor: Optional[float] = None
        # H-5: monotonic() timestamp of the first tick at/through the hard
        # max_loss cap in the current run; None while not at the hard stop or
        # after price recovers back inside it. Mirrors _first_breach_ts but is
        # on the tighter hard_stop_confirm_sec window. Not persisted (re-arms
        # from the first post-restart tick).
        self._first_hardstop_ts: Optional[float] = None

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

    def _active_tier(self, peak_px: float) -> RetraceTier:
        """Find the highest active retrace tier based on favorable excursion.

        Tiers are selected from the peak favorable price (``peak_px``), not the
        current mark: a retracement should use the loosest tier the trade has
        EARNED, so a spike that briefly cleared a high tier doesn't snap the
        floor back to the tight default on the very next down-tick.
        """
        upct = self._unrealized_pct(peak_px)
        active = RetraceTier(0.0, self.policy.retrace_threshold)  # default
        for tier in self.policy.phase2_tiers:
            if upct >= tier.pct_above_entry:
                active = tier
        return active

    def _effective_max_loss(self) -> float:
        """Effective SPOT-% stop: min(regime/fixed cap, ATR cap, ROE/lev cap).

        Pure computation — no state mutation. Shared by check() and status().

        The ATR stop may only WIDEN the stop up to the configured
        ``atr_stop_ceiling_pct``; it must never OVERRIDE a tighter regime
        ``max_loss_pct`` (live sets trend=0.8% / non-trend=0.4% via
        ``select_exit_params``). Previously the ATR branch replaced
        ``spot_cap`` wholesale, so an ATR value above the regime cap clamped at
        the 3% ceiling meant the tight regime stop never bound. Taking the min
        with ``pol.max_loss_pct`` keeps the regime stop as the hard upper
        bound while still letting a calm regime benefit from the ATR floor.
        """
        pol = self.policy
        lev = max(1, self.leverage)
        # Regime/fixed spot cap (live passes a per-regime value; default is the
        # config max_loss_pct). This is the hard upper bound on loss width.
        regime_cap = pol.max_loss_pct if pol.max_loss_pct > 0 else float("inf")
        if pol.atr_stop_enabled and self.entry_atr_pct > 0:
            atr_cap = min(max(self.entry_atr_pct * pol.atr_stop_mult,
                              pol.atr_stop_floor_pct),
                          pol.atr_stop_ceiling_pct)
            # ATR can only widen up to the regime cap, never override a tighter
            # regime stop: the binding spot cap is the smaller of the two.
            spot_cap = min(regime_cap, atr_cap)
        else:
            spot_cap = regime_cap
        roe_cap = (pol.max_loss_roe_pct / lev) if pol.max_loss_roe_pct > 0 else float("inf")
        spot_cap = spot_cap if spot_cap > 0 else float("inf")
        return min(spot_cap, roe_cap)

    def _phase_label(self) -> str:
        """Phase 1/2 label based on PEAK favorable excursion vs protect_pct.

        Phase 2 arms once and the floor never loosens after, so the label must
        track the peak the trade reached, not the current mark — otherwise a
        phase-2 position retracing back through protect_pct would be
        mislabeled "phase1" while its floor is still a phase-2 trail.
        """
        peak_pct = self._peak_profit_pct()
        return "phase2" if peak_pct >= self.policy.protect_pct else "phase1"

    # ── Direction helpers (long=+1, short=-1) ──────────────────────────
    # Every price/PnL comparison in check() can be expressed with a single
    # sign `sgn`: favorable moves scale by +sgn, adverse moves by -sgn. This
    # eliminates the long/short mirror duplication.
    def _sgn(self) -> int:
        return 1 if self.is_long() else -1

    def _peak_profit_pct(self) -> float:
        """Favorable peak excursion as a positive SPOT % (0 if peak at/under entry)."""
        return self._sgn() * (self.peak_px - self.entry_px) / self.entry_px * 100

    def _favorable_pct(self, px: float) -> float:
        """Signed favorable % for `px` vs entry: positive in profit, negative in loss."""
        return self._sgn() * (px - self.entry_px) / self.entry_px * 100

    def _hard_stop_floor(self, effective_max_loss: float) -> float:
        """Phase-1 hard stop price for the configured effective SPOT-% loss."""
        return self.entry_px * (1 - self._sgn() * effective_max_loss / 100)

    def _trailing_floor(self, retrace: float) -> float:
        """Phase-2 trailing floor: entry + sgn * favorable_range * (1-retrace)."""
        favorable_range = self._sgn() * (self.peak_px - self.entry_px)
        return self.entry_px + self._sgn() * favorable_range * (1 - retrace)

    def _smooth_phase2_floor(self, retrace: float, effective_max_loss: float) -> float:
        """Phase-2 floor ramped from the hard stop up to the full trail.

        Inert when ``smooth_transition_enabled`` is False or the band is
        non-positive: returns the plain trailing floor (byte-identical to the
        baseline). Otherwise linearly interpolates (in favorable-offset space,
        so it is correct for both longs and shorts) between the hard-stop floor
        and the full trailing floor as peak profit travels from ``protect_pct``
        to ``protect_pct + smooth_band_pct``. The interpolation factor is 0 at
        arm (floor = hard stop) and 1 once the band is exhausted (floor = full
        trail). The downstream ``_monotonic_clamp`` still guarantees the ramped
        floor never loosens relative to the prior tick.
        """
        pol = self.policy
        trail = self._trailing_floor(retrace)
        if not pol.smooth_transition_enabled or pol.smooth_band_pct <= 0:
            return trail
        peak_pct = self._peak_profit_pct()
        progress = (peak_pct - pol.protect_pct) / pol.smooth_band_pct
        progress = 0.0 if progress < 0.0 else (1.0 if progress > 1.0 else progress)
        hard = self._hard_stop_floor(effective_max_loss)
        return hard + (trail - hard) * progress

    def _apply_breakeven(self, floor: float) -> float:
        """Clamp the floor to the breakeven lock once peak profit arms it."""
        pol = self.policy
        if pol.breakeven_trigger_pct > 0 and self._peak_profit_pct() >= pol.breakeven_trigger_pct:
            be_px = self.entry_px * (1 + self._sgn() * pol.breakeven_lock_pct / 100)
            return max(floor, be_px) if self.is_long() else min(floor, be_px)
        return floor

    def _monotonic_clamp(self, floor: float, prev_floor: Optional[float]) -> float:
        """Floor must never loosen: max(prev) for longs, min(prev) for shorts."""
        if prev_floor is None:
            return floor
        return max(floor, prev_floor) if self.is_long() else min(floor, prev_floor)

    def check(self, mark_px: float, index_px: Optional[float] = None) -> ExitVerdict:
        """Evaluate DSL floor against current mark price. Call on every tick.

        A-F5 (deep audit 2026-08-28): ``index_px`` is the exchange oracle /
        index price for the same coin. Floor-breach EXIT verdicts require the
        index price to also be through the floor; the raw mid (allMids) can be
        pushed through a floor by a single-book wick, but the oracle index is a
        multi-source blend that a wick cannot move. When ``index_px`` is None
        or unusable the confirmation degrades to the breach_confirm_sec time
        gate alone (fail-open to mid; the exchange-side backup SL remains the
        hard backstop). Peak/floor tracking still follows the mid.
        """
        # Flush a deferred floor move from a prior tick if the throttle
        # interval has elapsed; _request_save re-checks the window and keeps
        # the dirty flag set otherwise, so a burst of ticks only writes once
        # per _MIN_SAVE_INTERVAL_SEC. This runs before the verdict so a crash
        # between ticks still leaves the last floor persisted within one
        # interval.
        if _SAVE_DIRTY:
            _request_save(force=False)
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

        # Audit 2026-09-10 (ADA MFE loss): a new favorable peak previously
        # never requested a save (the comment was "peak rebuilds"), so a
        # restart rehydrated a STALE peak from the last floor/register save
        # and the tracker could never rebuild it once price had rolled over
        # (ADA printed mfe=1.14% after restart vs a true 1.40% peak seen
        # pre-restart). Persist peak advances through the same coalesced
        # throttle as floor moves — at most one state write per
        # _MIN_SAVE_INTERVAL_SEC process-wide, so tick-rate I/O is unchanged
        # in practice while a crash/restart loses at most one interval of
        # peak advance instead of the whole favorable excursion.
        if peak_changed:
            _request_save(force=False)

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
        # Track whether ATR was active for the exit-reason string.
        atr_active = pol.atr_stop_enabled and self.entry_atr_pct > 0
        effective_max_loss = self._effective_max_loss()
        # Display-only: the raw spot cap before ROE clamping (for reason string).
        if atr_active:
            spot_cap_display = min(max(self.entry_atr_pct * pol.atr_stop_mult,
                                       pol.atr_stop_floor_pct),
                                   pol.atr_stop_ceiling_pct)
        else:
            spot_cap_display = pol.max_loss_pct if pol.max_loss_pct > 0 else float("inf")
        # Reason string surfaces both inputs so it's obvious post-hoc
        # which cap was binding for a given exit.

        # ── Stale-flat timeout ────────────────────────────────────────
        # Only for positions that never armed phase-2: peak profit < protect.
        if pol.stale_flat_timeout_minutes > 0 and elapsed_min >= pol.stale_flat_timeout_minutes:
            peak_profit = self._peak_profit_pct()
            if peak_profit < pol.protect_pct:
                _record_exit("stale_flat_timeout")
                _request_save(force=True)
                return self._verdict(
                    exit=True,
                    reason=(f"stale_flat_timeout ({elapsed_min:.0f}min below protect; "
                            f"peak {peak_profit:.2f}% < {pol.protect_pct}%)"),
                    floor_price=None, peak_price=self.peak_px, phase="timeout",
                    unrealized_pct=upct,
                )

        # ── Time-based scratch exit (E3, P2) ──────────────────────────
        # Prunes a chop position that never armed phase-2 but printed a small
        # favorable pulse now fading back to flat. Profit-only path: never
        # masks a loss (upct must be > 0), the pulse must have reached
        # time_scratch_min_peak_pct, the giveback from peak must be material,
        # and phase-2 must be un-armed (once peak >= protect the trailing
        # floor owns the exit). DEFAULT OFF (inert).
        if (pol.time_scratch_enabled
                and elapsed_min >= pol.time_scratch_minutes):
            peak_profit = self._peak_profit_pct()
            if (peak_profit < pol.protect_pct
                    and peak_profit >= pol.time_scratch_min_peak_pct
                    and upct > 0
                    and (peak_profit - upct) >= pol.time_scratch_giveback_pct):
                _record_exit("time_scratch")
                _request_save(force=True)
                return self._verdict(
                    exit=True,
                    reason=(f"time_scratch ({elapsed_min:.0f}min; peak {peak_profit:.2f}% "
                            f"gave back to {upct:.2f}%)"),
                    floor_price=None, peak_price=self.peak_px, phase="timeout",
                    unrealized_pct=upct,
                )

        # ── Hard timeout ──────────────────────────────────────────────
        if elapsed_min >= pol.hard_timeout_minutes:
            _record_exit("hard_timeout")
            _request_save(force=True)
            return self._verdict(
                exit=True, reason=f"hard_timeout ({elapsed_min:.0f}min)",
                floor_price=None, peak_price=self.peak_px, phase="timeout",
                unrealized_pct=upct,
            )

        # ── Compute floor ───────────────────────────────────────────
        # Floor only moves in the favorable direction (up for longs, down for
        # shorts) — once it locks profit it never gives it back. retrace_used
        # is logged on every floor change so the dynamic trail can be verified.
        # `sgn` (+1 long / -1 short) lets one expression cover both sides.
        sgn = self._sgn()
        profit_pct = self._favorable_pct(mark_px)   # >0 in profit, <0 in loss
        loss_pct = -profit_pct                       # >0 when losing

        # Max loss check (uses leverage-aware effective floor).
        # Use isclose on the boundary so a mark sitting exactly at the hard
        # stop (within floating-point noise) reports max_loss rather than
        # falling through to the phase-1 floor_breach — the hard stop must
        # win on priority even when the two floors coincide.
        if loss_pct >= effective_max_loss or math.isclose(
            loss_pct, effective_max_loss, rel_tol=1e-9, abs_tol=1e-9
        ):
            roe_loss = loss_pct * lev
            # H-5 (supplemental audit): wick guard for the hard stop. The
            # immediate-return below fired on the FIRST mid tick at the cap; a
            # single order-book wick that the index (oracle blend) never
            # traded at could market-close a position. Require (a) the breach
            # to persist for hard_stop_confirm_sec and (b) when a healthy index
            # price is available, the index to ALSO be through the hard-stop
            # floor. Kept short (default 1s): the hard stop bounds catastrophe
            # and the exchange-side bracket SL remains the ultimate net, so a
            # fail-open on missing index still exits, only slightly delayed.
            _hs_floor = self._hard_stop_floor(effective_max_loss)
            now_hs = time.monotonic()
            if self._first_hardstop_ts is None:
                self._first_hardstop_ts = now_hs
            hs_elapsed = now_hs - self._first_hardstop_ts
            hs_time_ok = (
                pol.hard_stop_confirm_sec <= 0
                or hs_elapsed >= pol.hard_stop_confirm_sec
            )
            hs_index_ok = True
            hs_index_note = ""
            try:
                _hs_idx = float(index_px) if index_px is not None else 0.0
            except (TypeError, ValueError):
                _hs_idx = 0.0
            if _hs_idx > 0.0 and math.isfinite(_hs_idx):
                hs_index_ok = sgn * (_hs_idx - _hs_floor) <= 0 or math.isclose(
                    sgn * (_hs_idx - _hs_floor), 0.0, abs_tol=1e-12
                )
                hs_index_note = (
                    ", idx-confirmed" if hs_index_ok
                    else f", mid-only wick? idx={_hs_idx:.6g} safe-side of stop"
                )
            if hs_time_ok and hs_index_ok:
                _record_exit("max_loss")
                _record_stop_tuning_shadow(
                    self, pol, effective_max_loss, loss_pct, roe_loss,
                    atr_active, spot_cap_display)
                _request_save(force=True)
                return self._verdict(
                    exit=True,
                    reason=(f"max_loss ({loss_pct:.2f}% spot / {roe_loss:.1f}% ROE "
                            f">= {effective_max_loss:.2f}% spot cap; "
                            f"spot_cap={spot_cap_display:.2f}{'[atr]' if atr_active else ''}, "
                            f"roe_cap={pol.max_loss_roe_pct}/{lev}x"
                            f"{', held %.1fs' % hs_elapsed if pol.hard_stop_confirm_sec > 0 else ''}"
                            f"{hs_index_note})"),
                    floor_price=_hs_floor,
                    peak_price=self.peak_px, phase="phase1", unrealized_pct=upct,
                )
            # Not yet confirmed: hold this tick (the exchange backup SL is the
            # net for a true gap), but log the suppressed wick so the guard is
            # observable. Do NOT reset the timer — if the breach is real the
            # next tick confirms; a tick back inside the cap resets it below.
            logger.info(
                f"[dsl:wick] {self.coin} {self.side} mark={mark_px:.6g} at hard "
                f"stop floor={_hs_floor:.6g} — holding for confirm "
                f"({hs_elapsed:.1f}/{pol.hard_stop_confirm_sec:.1f}s{hs_index_note}); "
                f"exchange backup SL remains the net."
            )
        else:
            # Price recovered back inside the hard-stop cap → reset the H-5
            # confirmation timer so the next touch re-arms from zero.
            self._first_hardstop_ts = None

        retrace_used = 0.0
        # Phase 2 arms on the PEAK favorable excursion, not the current mark.
        # This matches _phase_label (L621), _active_tier (tier by peak) and the
        # noise-band gate, all of which already key off the peak: a spike that
        # cleared protect_pct must keep trailing the peak on retracement rather
        # than snapping back to the phase-1 hard floor while the label already
        # reads "phase2". The floor is monotonic-clamped regardless, so this
        # only makes the activation criterion consistent (and keeps a restart-
        # restored tracker whose peak>=protect but mark<protect in phase2).
        if self._peak_profit_pct() >= pol.protect_pct:
            # Phase 2: floor trails peak by (1-retrace) of the favorable range.
            tier = self._active_tier(self.peak_px)  # Use PEAK for tier, not current
            retrace_used = tier.retrace_threshold
            # P2 candidate: ramp the floor hard→trail across smooth_band_pct
            # instead of snapping it at arm. Inert (== plain trail) when the
            # smooth transition is disabled (default) — see ExitPolicy.
            floor = self._smooth_phase2_floor(tier.retrace_threshold,
                                              effective_max_loss)
        else:
            # Phase 1: floor at the effective hard stop.
            floor = self._hard_stop_floor(effective_max_loss)

        # ── Breakeven ratchet (guaranteed-profit lock) ─────────────────
        floor = self._apply_breakeven(floor)

        # Floor must never loosen (max for longs / min for shorts vs previous).
        prev_floor = self._last_floor
        floor = self._monotonic_clamp(floor, prev_floor)

        self._last_floor = floor
        # Use a relative tolerance so floating-point noise between two
        # essentially-equal floors doesn't trigger a spurious save/log (which
        # also amplifies lock contention with other processes).
        floor_moved = prev_floor is None or not math.isclose(
            prev_floor, floor, rel_tol=1e-9, abs_tol=1e-12
        )
        if floor_moved:
            # Throttled in-process write: peak is a ratchet that rebuilds from
            # marks after restart, so only a floor move needs persistence, and
            # even that is coalesced to once per _MIN_SAVE_INTERVAL_SEC. A
            # deferred flush at the top of the next check() tick catches it if
            # the interval hasn't elapsed yet. Exit verdicts below force-save.
            _record_floor_move()
            _request_save(force=False)
        # Log every floor update so the dynamic trail can be verified (logging
        # doesn't touch the lock file, so this stays on every floor change):
        if floor_moved or peak_changed:
            logger.info(
                f"[dsl:floor] {self.coin} {self.side} "
                f"phase={'phase2' if retrace_used > 0 else 'phase1'} "
                f"entry={self.entry_px:.6g} mark={mark_px:.6g} "
                f"peak={self.peak_px:.6g} retrace={retrace_used*100:.0f}% "
                f"floor={floor:.6g} "
                f"(floor_moved={floor_moved}, peak_changed={peak_changed}, "
                f"prev_floor={prev_floor})"
            )

        # ── Floor breach check ────────────────────────────────────────
        # Use <=/>= (not strict inequality) so a mark sitting exactly on the
        # floor counts as a breach — matches exchange trigger-order semantics
        # and avoids the local/remote boundary disagreeing at the tick. A mark
        # on the adverse side of the floor (sgn*(mark-floor) <= 0) is a breach.
        breached = sgn * (mark_px - floor) <= 0 or math.isclose(
            sgn * (mark_px - floor), 0.0, abs_tol=1e-12
        )
        # Patch A — noise-band suppression (sub-first-tier only). The hard
        # max_loss stop already returned above; this only governs the trailing
        # give-back of a barely-green position. If peak profit hasn't yet cleared
        # the first phase-2 tier AND the current pull-back from peak is inside
        # the ATR noise band, treat it as NOT breached (hold) so we don't concede
        # inside the noise. Requires an ATR captured at entry; degrades to
        # current behavior when absent.
        if breached and pol.noise_band_enabled and self.entry_atr_pct > 0:
            first_tier_pct = min((t.pct_above_entry for t in pol.phase2_tiers), default=3.0)
            peak_profit_pct = self._peak_profit_pct()
            pullback_pct = (abs(self.peak_px - mark_px) / self.entry_px) * 100
            band = pol.noise_band_atr_mult * self.entry_atr_pct
            if peak_profit_pct < first_tier_pct and pullback_pct <= band:
                self.consecutive_breaches = 0
                self._first_breach_ts = None
                self._last_floor = floor
                return self._verdict(
                    exit=False, reason="noise_band_hold", floor_price=floor,
                    peak_price=self.peak_px,
                    phase="phase1", unrealized_pct=upct,
                )
        if breached:
            self.consecutive_breaches += 1
            # Time-based confirmation gate (P2-7): arm the timer on the
            # first breach tick; only exit once the breach has persisted
            # for breach_confirm_sec. A recovering tick below resets the
            # timer via the else branch. This is clock-based rather than
            # tick-count based, so WS feed rate doesn't change the gate.
            now_mono = time.monotonic()
            if self._first_breach_ts is None:
                self._first_breach_ts = now_mono
            breach_elapsed = now_mono - self._first_breach_ts
            time_gate_ok = (
                pol.breach_confirm_sec <= 0
                or breach_elapsed >= pol.breach_confirm_sec
            )
            count_gate_ok = (
                self.consecutive_breaches >= pol.consecutive_breaches_required
            )
            # A-F5: wick confirmation against the INDEX price. The mid is a
            # single order-book snapshot; a transient wick can push it through
            # the floor for one poll even though the index (oracle blend) never
            # traded there. If a usable index price is supplied, require it to
            # also be through the floor to exit; a healthy index ABOVE the floor
            # while mid pokes below is the classic wick → hold and keep waiting
            # (the time gate keeps running). No usable index → degrade to the
            # time/count gates on mid (fail-open; exchange backup SL is the net).
            index_ok = True
            index_note = ""
            try:
                _idx = float(index_px) if index_px is not None else 0.0
            except (TypeError, ValueError):
                _idx = 0.0
            if _idx > 0.0 and math.isfinite(_idx):
                index_ok = sgn * (_idx - floor) <= 0 or math.isclose(
                    sgn * (_idx - floor), 0.0, abs_tol=1e-12
                )
                index_note = ", idx-confirmed" if index_ok else f", mid-only wick? idx={_idx:.6g} above floor"
            if time_gate_ok and count_gate_ok and index_ok:
                _record_exit("floor_breach")
                _request_save(force=True)
                held_for = (
                    f", held {breach_elapsed:.1f}s"
                    if pol.breach_confirm_sec > 0 else ""
                )
                return self._verdict(
                    exit=True,
                    reason=(
                        f"floor_breach ({self.consecutive_breaches}x consec"
                        f"{held_for}, floor={floor:.6g}{index_note})"
                    ),
                    floor_price=floor, peak_price=self.peak_px,
                    phase=self._phase_label(),
                    unrealized_pct=upct,
                )
            if not index_ok:
                # Mid breached but index didn't — log the suppressed wick so
                # the behavior is observable, but do NOT reset the time gate:
                # if the breach is real the next tick's index will confirm.
                logger.info(
                    f"[dsl:wick] {self.coin} {self.side} mid={mark_px:.6g} "
                    f"breached floor={floor:.6g} but index={_idx:.6g} did not — "
                    f"holding (A-F5 wick suppression, {breach_elapsed:.1f}s elapsed)."
                )
        else:
            self.consecutive_breaches = 0
            self._first_breach_ts = None

        return self._verdict(
            exit=False, reason="", floor_price=self._last_floor,
            peak_price=self.peak_px,
            phase=self._phase_label(),
            unrealized_pct=upct,
        )

    def status(self, mark_px: float) -> dict[str, Any]:
        """Return a READ-ONLY DSL status snapshot (for logging/MCP/dashboard).

        This MUST NOT advance peak_px/_last_floor, increment the breach
        counter, or persist state — those side effects belong exclusively to
        check(). It reports the position as it stands RIGHT NOW from the last
        check() evaluation, plus an indicative `would_exit` flag computed
        against the current floor without mutating anything.
        """
        is_long = self.is_long()
        upct = self._unrealized_pct(mark_px)
        pol = self.policy
        elapsed_min = (time.time() - self.entry_time) / 60

        # Floor: prefer the last floor established by check(); fall back to the
        # phase-1 hard-stop floor if check() has never run for this tracker.
        if self._last_floor is not None:
            floor_px = self._last_floor
        else:
            emax = self._effective_max_loss()
            floor_px = (self.entry_px * (1 - emax / 100) if is_long
                        else self.entry_px * (1 + emax / 100))

        # Indicative exit flags (no counter advancement, no noise suppression —
        # this is a snapshot, not a trading decision).
        loss_pct = -upct if is_long else upct  # positive when losing
        at_hard_stop = loss_pct >= self._effective_max_loss()
        at_floor = ((mark_px <= floor_px) if is_long else (mark_px >= floor_px))
        would_exit = at_hard_stop or at_floor or elapsed_min >= pol.hard_timeout_minutes
        if at_hard_stop:
            exit_reason = "max_loss"
        elif elapsed_min >= pol.hard_timeout_minutes:
            exit_reason = "hard_timeout"
        elif at_floor:
            exit_reason = "at_floor"
        else:
            exit_reason = ""

        return {
            "coin": self.coin,
            "side": self.side,
            "entry_px": self.entry_px,
            "mark_px": mark_px,
            "peak_px": self.peak_px,
            "floor_px": floor_px,
            "unrealized_pct": round(upct, 2),
            "phase": self._phase_label(),
            "consecutive_breaches": self.consecutive_breaches,
            "hold_min": round(elapsed_min, 1),
            "would_exit": would_exit,
            "exit_reason": exit_reason,
        }


# ── Global tracker registry ──────────────────────────────────────────

_active_positions: dict[str, DSLTracker] = {}
_loaded_from_disk = False
# H-P1 follow-up: latches True when a force-reload finds at least one on-disk
# state candidate that exists but cannot be read/migrated (live + .bak both
# unusable), versus the legitimate "no state file has ever existed" empty
# registry. find_dsl_bracket_trigger reads this so a corrupt tracker cannot be
# mistaken for "not a DSL bracket" (which would fail open on a cancel).
_last_force_load_corrupt = False

# P1-1: trackers whose exchange SL oid is SUSPECTED dead after a batchModify
# whose response was lost. In-memory only (a restart re-derives health from the
# one-shot startup verification in backfill_brackets_from_exchange). Keys are
# tracker keys "<coin>_<side>". backfill queries openOrders whenever this set is
# non-empty, so a lost-response modify self-heals instead of pinning a dead oid
# that freezes the trailing mover forever.
_suspect_sl_keys: set[str] = set()
# One-shot, per-process: verify already-held sl_oids against the exchange on the
# first backfill after load. The steady-state gate stays "query only when an oid
# is missing/suspect", so normal ticks never add network calls.
_held_oids_verified = False


def mark_sl_oid_suspect(coin: str, side: str) -> None:
    """Flag a tracker's resting SL oid as possibly dead (lost modify response).

    Never raises. Best-effort; the worst case of a missing flag is the pre-fix
    behaviour, never a wrong order action.
    """
    try:
        _suspect_sl_keys.add(f"{coin}_{side}")
    except Exception:
        pass


def _clear_sl_suspect(tracker: DSLTracker) -> None:
    _suspect_sl_keys.discard(f"{tracker.coin}_{tracker.side}")


def _tracker_to_dict(t: DSLTracker) -> dict[str, Any]:
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


def _migrate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Bring an on-disk state payload up to ``_STATE_VERSION`` in place.

    Each ``_migrate_vN_to_vN+1`` is a pure structural transform: it adds
    fields introduced in the newer schema with safe defaults and never
    guesses market data. Unknown future versions are left untouched and
    warned about (forward-compat: a downgraded daemon shouldn't corrupt a
    newer file). The migration is deliberately idempotent so re-running it
    on an already-current payload is a no-op.
    """
    if not isinstance(payload, dict):
        raise ValueError("state payload is not a JSON object")
    raw_version = payload.get("version")
    try:
        version = int(raw_version) if raw_version is not None else 1
    except (TypeError, ValueError):
        logger.warning(
            f"[dsl] state file has unparseable version {raw_version!r}; "
            f"treating as v1"
        )
        version = 1

    if version > _STATE_VERSION:
        logger.warning(
            f"[dsl] state file version {version} is newer than this binary "
            f"(expects v{_STATE_VERSION}); loading without migration — a "
            f"daemon downgrade may be in progress"
        )
        return payload

    if version < 2:
        logger.info("[dsl] migrating state file v1 → v2 (bracket order fields)")
        payload = _migrate_v1_to_v2(payload)
        version = 2

    # Future migrations chain here:
    # if version < 3:
    #     payload = _migrate_v2_to_v3(payload); version = 3
    payload["version"] = _STATE_VERSION
    return payload


def _migrate_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """v1 → v2: add exchange bracket order IDs / prices (all default None).

    v1 was written before the static backup-SL/TP reconciler existed, so
    trackers carried no ``sl_oid``/``sl_px``/``sl_size``/``tp_oid``/
    ``tp_px``. These are backfilled by ``reconcile_bracket_orders()`` on
    the next rehydrate — migration just needs the keys present with None
    so ``_tracker_from_dict`` doesn't KeyError on a strict reader.
    """
    for d in payload.get("positions", []) or []:
        if not isinstance(d, dict):
            continue
        d.setdefault("sl_oid", None)
        d.setdefault("sl_px", None)
        d.setdefault("sl_size", None)
        d.setdefault("tp_oid", None)
        d.setdefault("tp_px", None)
    payload["version"] = 2
    return payload


def _tracker_from_dict(d: dict[str, Any]) -> DSLTracker:
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
        # A-F5: fallback default moved to 4.0s (see ExitPolicy.breach_confirm_sec).
        breach_confirm_sec=float(pol_raw.get("breach_confirm_sec", 4.0) or 0.0),
        # H-5: hard max_loss stop wick-guard window (default 1.0s; see
        # ExitPolicy.hard_stop_confirm_sec).
        hard_stop_confirm_sec=float(pol_raw.get("hard_stop_confirm_sec", 1.0) or 0.0),
        breakeven_trigger_pct=pol_raw.get("breakeven_trigger_pct", ExitPolicy.breakeven_trigger_pct),
        breakeven_lock_pct=pol_raw.get("breakeven_lock_pct", ExitPolicy.breakeven_lock_pct),
        atr_stop_enabled=pol_raw.get("atr_stop_enabled", ExitPolicy.atr_stop_enabled),
        # Audit 2026-09-06 (E5, P2): a persisted policy missing
        # stale_flat_timeout_minutes must hydrate to the canonical ExitPolicy
        # default (480), not 0 which silently DISABLES the stale-flat guard
        # after a restart. `or 0.0` still honours an explicit 0 (disabled).
        stale_flat_timeout_minutes=float(
            pol_raw.get("stale_flat_timeout_minutes", ExitPolicy.stale_flat_timeout_minutes)
            or 0.0),
        atr_stop_mult=pol_raw.get("atr_stop_mult", ExitPolicy.atr_stop_mult),
        atr_stop_floor_pct=pol_raw.get("atr_stop_floor_pct", ExitPolicy.atr_stop_floor_pct),
        atr_stop_ceiling_pct=pol_raw.get("atr_stop_ceiling_pct", ExitPolicy.atr_stop_ceiling_pct),
        noise_band_enabled=pol_raw.get("noise_band_enabled", ExitPolicy.noise_band_enabled),
        noise_band_atr_mult=pol_raw.get("noise_band_atr_mult", ExitPolicy.noise_band_atr_mult),
        # Audit 2026-09-06 (E4, P2): preserve the smooth-transition knob across
        # rehydrate (previously dropped → an enabled ramp silently reset to OFF
        # after a restart). Absent in old state files → inert default.
        smooth_transition_enabled=pol_raw.get("smooth_transition_enabled", ExitPolicy.smooth_transition_enabled),
        smooth_band_pct=pol_raw.get("smooth_band_pct", ExitPolicy.smooth_band_pct),
        # Audit 2026-09-06 (E3, P2): preserve the time-scratch knob across
        # rehydrate; absent in old state files → inert default.
        time_scratch_enabled=pol_raw.get("time_scratch_enabled", ExitPolicy.time_scratch_enabled),
        time_scratch_minutes=pol_raw.get("time_scratch_minutes", ExitPolicy.time_scratch_minutes),
        time_scratch_min_peak_pct=pol_raw.get("time_scratch_min_peak_pct", ExitPolicy.time_scratch_min_peak_pct),
        time_scratch_giveback_pct=pol_raw.get("time_scratch_giveback_pct", ExitPolicy.time_scratch_giveback_pct),
    )
    t = DSLTracker(d["coin"], d["side"], float(d["entry_px"]),
                   float(d.get("entry_time") or time.time()), policy,
                   leverage=int(d.get("leverage", 1) or 1),
                   entry_atr_pct=float(d.get("entry_atr_pct", 0.0) or 0.0),
                   entry_regime=str(d.get("entry_regime") or ""))
    t.peak_px = float(d.get("peak_px", d["entry_px"]))
    t.consecutive_breaches = int(d.get("consecutive_breaches", 0))
    # Audit 2026-09-06 (E5, P2): validate a persisted last_floor before
    # trusting it as the ratchet anchor. The floor is always a positive PRICE
    # (long: a stop below entry that ratchets up; short: a stop above entry
    # that ratchets down). A 0/negative/non-finite value is corrupt or hand-
    # edited and means "no stop at all" — the loosest, most dangerous state
    # (the ratchet would never fire a floor-breach exit). Drop it to None so
    # the floor re-seeds conservatively from the bracket SL / next check().
    lf = d.get("last_floor")
    if lf is not None:
        try:
            lf = float(lf)
        except (TypeError, ValueError):
            lf = None
        else:
            if not math.isfinite(lf) or lf <= 0.0:
                lf = None
    t._last_floor = lf
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


# ── Save retry policy ─────────────────────────────────────────────────
# A transient disk hiccup (EINTR, ENOSPC flap, NFS lock stall) must not
# silently lose the floor ratchet. Retry a small number of times with an
# exponential backoff while still holding LOCK_EX (no other writer can
# make progress anyway), then give up, bump the Prometheus counter, push
# a risk-category Feishu card, and leave _SAVE_DIRTY set so the next tick
# retries. Override via env if the deployment's disk is known-slow.
# R13-B1: read from canonical dsl_state_io; legacy env still wins.
_SAVE_MAX_ATTEMPTS = int(
    os.environ.get("HERMES_DSL_SAVE_MAX_ATTEMPTS")
    or cfg_get("dsl_state_io.save_max_attempts", config={})
)
_SAVE_BACKOFF_BASE_SEC = float(
    os.environ.get("HERMES_DSL_SAVE_BACKOFF_BASE_SEC")
    or cfg_get("dsl_state_io.save_backoff_base_sec", config={})
)
# R13-B12: exponential backoff growth factor between save retries (was the
# bare literal 3 in `3 ** attempt`). No legacy env channel — reachable via
# HERMES_CFG_DSL_STATE_IO__SAVE_BACKOFF_FACTOR / the agent-config dict.
_SAVE_BACKOFF_FACTOR = int(cfg_get("dsl_state_io.save_backoff_factor", 3, config={}))


# Audit 2026-09-06 (E5, P2): state-file resilience. A corrupt .dsl-state.json
# used to be swallowed with a warning and the registry silently reset to empty
# on restart (ratchet floors lost → stop loss could ratchet to zero). A
# single-generation .bak (the last-known-good live file) plus quarantine of
# the corrupt byte-for-byte copy now gives a deterministic recovery path.
# Everything in this block is best-effort: quarantine/alert/metric failures
# must never break the load/save hot path. The .bak path is derived from the
# live DSL_STATE_FILE at call time (not cached) so test monkeypatching of
# DSL_STATE_FILE redirects the backup alongside it.


def _quarantine_corrupt_state(path: str) -> None:
    """Move a corrupt state file aside to ``<path>.corrupt-<ms>``. Best-effort.

    Audit 2026-09-06 (E5, P2). Also bumps the corrupt-isolation counter and
    pushes a risk-category Feishu card; all three effects are individually
    guarded so a notify/metrics hiccup cannot break the load path.
    """
    try:
        quar = f"{path}.corrupt-{int(time.time() * 1000)}"
        shutil.move(path, quar)
        logger.error(f"[dsl] corrupt state file quarantined: {path} -> {quar}")
    except OSError as qe:
        logger.error(f"[dsl] failed to quarantine corrupt state file {path}: {qe}")
        quar = None
    try:
        from hermes_trader.metrics import DSL_STATE_CORRUPT_ISOLATIONS

        DSL_STATE_CORRUPT_ISOLATIONS.inc()
    except Exception as metric_e:
        # H-P3: a corrupt tracker isolation is a fund-safety event; a dropped
        # metric must at least be visible in logs.
        logger.error("[dsl] DSL_STATE_CORRUPT_ISOLATIONS metric inc failed: %r", metric_e)
    try:
        from hermes_trader import notify

        notify.send_card(
            "DSL 状态文件损坏已隔离",
            fields={
                "文件": path,
                "隔离副本": str(quar),
                "后续动作": "已尝试回退 .bak；若无 .bak 则以空注册表启动",
            },
            category="risk",
            level="danger",
            dedup_key="dsl-state-corrupt",
        )
    except Exception as alert_e:
        # H-P3: this danger card is the only notice that the stop-tracker
        # registry was corrupt; never let its delivery failure vanish.
        logger.error("[dsl] corrupt-state danger card failed for %s: %r", path, alert_e)


def _read_state_candidate(path: str) -> Optional[dict[str, Any]]:
    """Read+json.load one state-file candidate; quarantine and return None on
    failure (FileNotFoundError → None silently). Best-effort, never raises.

    Audit 2026-09-06 (E5, P2).
    """
    try:
        with open(path) as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            raise ValueError("top-level JSON is not an object")
        return payload
    except FileNotFoundError:
        return None
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.error(f"[dsl] state candidate unreadable ({path}): {e}")
        _quarantine_corrupt_state(path)
        return None


def _rotate_state_bak_pre() -> None:
    """Before overwriting the live file, copy it to .bak (previous generation).

    Audit 2026-09-06 (E5, P2). Best-effort, never raises. Called under the
    LOCK_EX so no writer can replace the live file mid-copy.
    """
    try:
        if os.path.exists(DSL_STATE_FILE):
            shutil.copy2(DSL_STATE_FILE, DSL_STATE_FILE + ".bak")
    except OSError as e:
        logger.warning(f"[dsl] pre-write .bak rotation failed: {e}")


def _seed_state_bak_post() -> None:
    """After the first successful write, seed .bak from the fresh live file if
    no .bak exists yet (first-ever save has no predecessor to rotate).

    Audit 2026-09-06 (E5, P2). Best-effort, never raises.
    """
    try:
        if not os.path.exists(DSL_STATE_FILE + ".bak") and os.path.exists(DSL_STATE_FILE):
            shutil.copy2(DSL_STATE_FILE, DSL_STATE_FILE + ".bak")
    except OSError as e:
        logger.warning(f"[dsl] post-write .bak seed failed: {e}")


def _save_state() -> None:
    """Atomically write the tracker registry to disk. Best-effort — never raises.

    Retries up to ``_SAVE_MAX_ATTEMPTS`` with exponential backoff before
    giving up; on terminal failure bumps ``DSL_STATE_SAVE_ERRORS``, pushes
    a risk-category Feishu card, and leaves ``_SAVE_DIRTY`` set so the next
    tick retries. ``_LAST_SAVE_TS`` is always advanced so a burst of ticks
    doesn't hammer a sick disk on every call.
    """
    global _LAST_SAVE_TS, _SAVE_DIRTY
    _t0 = time.monotonic()
    payload = {
        "version": _STATE_VERSION,
        "saved_at": int(time.time() * 1000),
        "positions": [_tracker_to_dict(t) for t in _active_positions.values()],
    }
    lock_fd = None
    last_err: Optional[OSError] = None
    try:
        # Cross-process exclusive lock prevents lost updates when the trading
        # loop races a rehydrate/dashboard write through the same file. Held
        # across all retries — dropping and reacquiring it between attempts
        # would let another process interleave a stale write.
        lock_fd = os.open(DSL_STATE_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # Audit 2026-09-06 (E5, P2): rotate the current live file to .bak
        # before it is replaced, so a corrupt later write never destroys the
        # last-known-good copy. Held under LOCK_EX.
        _rotate_state_bak_pre()
        for attempt in range(_SAVE_MAX_ATTEMPTS):
            try:
                # atomic_io gives the durability contract the bare
                # open()+os.replace here used to lack: fsync the file before
                # the rename and fsync the directory after, so a power loss
                # mid-save cannot leave a zero-length/old state file.
                atomic_io.write_json_atomic(
                    DSL_STATE_FILE, payload, indent=None, fsync=True
                )
                last_err = None
                # Audit 2026-09-06 (E5, P2): first-ever save has no
                # predecessor to rotate — seed .bak from the fresh live file
                # so the very first save already leaves a recovery copy.
                _seed_state_bak_post()
                break
            except OSError as e:
                last_err = e
                if attempt + 1 < _SAVE_MAX_ATTEMPTS:
                    logger.warning(
                        f"[dsl] state save attempt {attempt + 1}/"
                        f"{_SAVE_MAX_ATTEMPTS} failed: {e}; retrying"
                    )
                    time.sleep(_SAVE_BACKOFF_BASE_SEC * (_SAVE_BACKOFF_FACTOR ** attempt))
                else:
                    logger.error(
                        f"[dsl] state save failed after "
                        f"{_SAVE_MAX_ATTEMPTS} attempts: {e}"
                    )
    except OSError as e:
        # Lock acquisition itself failed — no point retrying, the disk or
        # filesystem is unavailable at the open()/flock() layer.
        last_err = e
        logger.error(f"[dsl] could not acquire state lock for save: {e}")
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass
        _LAST_SAVE_TS = time.monotonic()
        if last_err is None:
            _SAVE_DIRTY = False
        # On failure set dirty so the next check() tick retries, even if this
        # was a force-save (register/deregister/exit) where _request_save
        # didn't pre-set it. _request_save() short-circuits on the interval.
        else:
            _SAVE_DIRTY = True
            try:
                from hermes_trader.metrics import DSL_STATE_SAVE_ERRORS

                DSL_STATE_SAVE_ERRORS.inc()
            except Exception:
                pass
            try:
                from hermes_trader import notify

                notify.send_card(
                    "DSL 状态落盘失败",
                    fields={
                        "文件": DSL_STATE_FILE,
                        "重试次数": str(_SAVE_MAX_ATTEMPTS),
                        "错误": repr(last_err),
                        "持仓数": str(len(_active_positions)),
                    },
                    category="risk",
                    level="danger",
                    dedup_key="dsl-state-save-failed",
                )
            except Exception as alert_e:
                # H-P3: a failed tracker persist means stop state may be lost
                # across restart; the danger card must not vanish silently.
                logger.error("[dsl] state-save-failed danger card failed: %r", alert_e)
        # P3-1: save latency (covers lock + all retries) and dirty gauge.
        try:
            from hermes_trader import metrics

            metrics.DSL_STATE_SAVE_DURATION.labels(
                outcome="ok" if last_err is None else "failed"
            ).observe(max(0.0, time.monotonic() - _t0))
            metrics.DSL_STATE_DIRTY.set(0.0 if last_err is None else 1.0)
        except Exception:
            pass


def _request_save(force: bool = False) -> None:
    """Coalesced state-save entry point used by check().

    ``force=True`` writes through immediately (exit verdict, structural
    register/deregister) and clears any pending dirty state. Otherwise the
    request is rate-limited to once per ``_MIN_SAVE_INTERVAL_SEC``; a floor
    move (or, since 2026-09-10, a favorable PEAK advance — see the peak-save
    fix in DSLTracker.check) that lands inside the window sets the dirty flag
    so the next tick flushes it, bounding restart loss to one interval.
    """
    global _SAVE_DIRTY
    if force:
        _save_state()
        return
    _SAVE_DIRTY = True
    if time.monotonic() - _LAST_SAVE_TS >= _MIN_SAVE_INTERVAL_SEC:
        _save_state()


def load_state(force: bool = False) -> None:
    """Load persisted trackers into `_active_positions`.

    Idempotent by default (skips after the first call in a process). Pass
    ``force=True`` from read-only consumers like the web dashboard that need
    to pick up disk state written by the trading-loop process; these reloads
    are throttled to once per ``_FORCE_LOAD_TTL_S`` so repeated dashboard
    polls don't contend with the loop's LOCK_EX writes on every request.
    """
    global _loaded_from_disk, _LAST_FORCE_LOAD_TS, _last_force_load_corrupt
    if _loaded_from_disk and not force:
        return
    if force:
        # Throttle: serve the in-memory copy when a force-reload ran recently.
        # This avoids clear()+LOCK_SH+file-read on every dashboard request.
        if time.monotonic() - _LAST_FORCE_LOAD_TS < _FORCE_LOAD_TTL_S:
            return
    _loaded_from_disk = True
    lock_fd = None
    payload = None
    # H-P1 follow-up: distinguish "no state file exists yet" (legitimate empty
    # registry) from "a state file exists but is unreadable/migratable" (must
    # fail closed for the cancel guard). Reset the corrupt latch for this load.
    saw_existing_candidate = False
    try:
        # Shared lock pairs with the exclusive lock in _save_state so a
        # force-reload never observes a torn write.
        lock_fd = os.open(DSL_STATE_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        # Audit 2026-09-06 (E5, P2): recovery chain live -> .bak. A corrupt
        # live file is quarantined (moved aside, alerted, counted) inside
        # _read_state_candidate and the single-generation .bak is tried in the
        # SAME load call — _loaded_from_disk is already latched True, so this
        # is the only point a fallback read can happen in this process.
        for candidate in (DSL_STATE_FILE, DSL_STATE_FILE + ".bak"):
            if os.path.exists(candidate):
                saw_existing_candidate = True
            raw = _read_state_candidate(candidate)
            if raw is None:
                continue
            try:
                payload = _migrate_payload(raw)
                break
            except (ValueError, TypeError) as e:
                logger.warning(
                    f"[dsl] state migration failed for {candidate}: {e}"
                )
                _quarantine_corrupt_state(candidate)
                payload = None
                continue
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass
    if payload is None:
        # Neither live nor .bak was readable/migratable (or neither exists).
        # Corrupt copies were already quarantined and alerted above; start
        # from an empty registry instead of crashing the trading loop or
        # running on stale in-memory trackers whose disk state is gone.
        _active_positions.clear()
        _refresh_positions_gauge()
        # Latch corrupt (vs legitimately-never-existed) ONLY on a force reload
        # so find_dsl_bracket_trigger can fail closed; the trading loop's own
        # non-force startup load keeps its tolerate-and-clear behavior.
        _last_force_load_corrupt = bool(force and saw_existing_candidate)
        return
    _last_force_load_corrupt = False
    # Record the successful reload time only AFTER reading the file so a
    # missing/empty file doesn't block the next attempt for a full TTL.
    if force:
        _LAST_FORCE_LOAD_TS = time.monotonic()
        _active_positions.clear()
    for d in payload.get("positions", []):
        try:
            t = _tracker_from_dict(d)
            _active_positions[f"{t.coin}_{t.side}"] = t
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"[dsl] skipping malformed tracker entry: {e}")
    if not force:
        logger.info(f"[dsl] rehydrated {len(_active_positions)} tracker(s) from disk")
    _refresh_positions_gauge()


def reset_force_load_throttle() -> None:
    """Reset the force-reload throttle timestamp (F23: public interface).

    Read-only consumers like the operator dashboard call this on an explicit
    ``?refresh=true`` so the next ``load_state(force=True)`` re-reads disk
    immediately instead of serving the throttled in-memory copy.
    """
    global _LAST_FORCE_LOAD_TS
    _LAST_FORCE_LOAD_TS = 0.0


def find_dsl_bracket_trigger(oid: int) -> Optional[tuple[str, str, str]]:
    """Return ``(coin, side, "sl"|"tp")`` if ``oid`` is a DSL-managed bracket.

    Shared guard for every cancel path (HTTP, MCP, autonomous loop): force a
    throttled shared-locked reload of the on-disk tracker registry first, so a
    process that does not own the trading loop still sees current oids. Raises
    on tracker-load failure so callers can fail closed; returns ``None`` when
    the oid is not a DSL-managed SL/TP trigger.
    """
    reset_force_load_throttle()
    load_state(force=True)
    if _last_force_load_corrupt:
        # The on-disk registry existed but could not be loaded from either the
        # live file or .bak. An empty table here is NOT proof the oid is not a
        # DSL bracket — refusing the cancel (fail closed) is the only safe
        # answer, matching this helper's contract and its HTTP/MCP callers.
        raise RuntimeError(
            "DSL tracker registry exists but could not be loaded (live + .bak "
            "unreadable/corrupt); cannot verify bracket ownership"
        )
    for _t in _active_positions.values():
        if oid in (_t.sl_oid, _t.tp_oid):
            return _t.coin, _t.side, ("sl" if oid == _t.sl_oid else "tp")
    return None


def active_tracker_snapshots() -> list[dict[str, Any]]:
    """Return public snapshot dicts of all active DSL trackers (F23).

    Replaces direct dashboard access to ``_active_positions`` /
    ``tracker._last_floor``. Call ``load_state(force=True)`` first to pick up
    disk state written by the trading-loop process.
    """
    out: list[dict[str, Any]] = []
    for key, t in _active_positions.items():
        out.append({
            "key": key, "coin": t.coin, "side": t.side,
            "entry_px": t.entry_px, "peak_px": t.peak_px,
            "floor_px": t._last_floor, "entry_time": t.entry_time,
            "consecutive_breaches": t.consecutive_breaches,
        })
    return out


def tracker_view(coin: str, side: str) -> Optional[dict[str, Any]]:
    """Return peak/floor/phase for one tracked position (F23 public accessor).

    Replaces dashboard poking ``_active_positions`` / ``tracker._last_floor``
    directly. ``phase`` is "phase2" once a trailing floor exists on the
    profit side of entry, else "phase1". Returns None when no tracker exists
    or no floor has been set yet.
    """
    t = _active_positions.get(f"{coin}_{side}")
    if t is None:
        return None
    floor = t._last_floor
    return {
        "peak_px": t.peak_px,
        "floor_px": floor,
        "phase": "phase2" if floor and (
            (side == "long" and floor > t.entry_px)
            or (side == "short" and floor < t.entry_px)
        ) else "phase1",
    }


def register_position(coin: str, side: str, entry_px: float,
                      entry_time: Optional[float] = None,
                      policy: Optional[ExitPolicy] = None,
                      leverage: int = 1,
                      entry_atr_pct: float = 0.0,
                      entry_regime: str = "") -> DSLTracker:
    """Register a new position for DSL tracking."""
    key = f"{coin}_{side}"
    if key in _active_positions:
        # Overwriting discards the existing tracker's peak/floor ratchet and
        # bracket oid state. This is normally a sign of a re-entry guard leak
        # or a double register — log loudly so it's visible rather than silent.
        old = _active_positions[key]
        logger.warning(
            f"[dsl] register_position OVERWRITES existing tracker {key} "
            f"(old entry={old.entry_px} peak={old.peak_px} floor={old._last_floor}); "
            f"ratchet state reset to new entry={entry_px}"
        )
    tracker = DSLTracker(coin, side, entry_px, entry_time or time.time(), policy,
                         leverage=leverage, entry_atr_pct=entry_atr_pct,
                         entry_regime=entry_regime)
    _active_positions[key] = tracker
    _save_state()
    _refresh_positions_gauge()
    atr_note = ""
    pol = tracker.policy
    if pol.atr_stop_enabled and entry_atr_pct > 0:
        width = min(max(entry_atr_pct * pol.atr_stop_mult, pol.atr_stop_floor_pct),
                    pol.atr_stop_ceiling_pct)
        atr_note = f" atr_stop={width:.2f}% ({pol.atr_stop_mult}x ATR {entry_atr_pct:.2f}%)"
    logger.info(f"[dsl] Registered {key} @ {entry_px} ({leverage}x){atr_note}")
    return tracker


def active_position_coins() -> dict[str, str]:
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
        _refresh_positions_gauge()
        logger.info(f"[dsl] Deregistered {key}")
        return True
    return False


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
    config can't be read.

    The result is cached for ``_POLICY_CACHE_TTL_S``: rehydrate can synthesize
    many trackers in one exchange scan and each previously re-read/re-parsed
    the config file. Config edits are operator-driven and rare, so a short TTL
    is safe while eliminating the per-tracker disk read.
    """
    global _POLICY_CACHE, _POLICY_CACHE_TS
    now = time.monotonic()
    if _POLICY_CACHE is not None and now - _POLICY_CACHE_TS < _POLICY_CACHE_TTL_S:
        return _POLICY_CACHE
    policy = _build_policy_from_config()
    _POLICY_CACHE = policy
    _POLICY_CACHE_TS = now
    return policy


def _build_policy_from_config() -> ExitPolicy:
    """Uncached config → ExitPolicy construction (see _policy_from_config)."""
    try:
        from hermes_trader.agents.config_store import read_agent_config
        dsl = read_agent_config().get("dsl_exit", {}) or {}
        tiers_raw = dsl.get("phase2_tiers")
        tiers = [RetraceTier(**t) for t in tiers_raw] if tiers_raw else None
        atr_cfg = dsl.get("atr_stop", {}) or {}
        noise_cfg = dsl.get("noise_band", {}) or {}
        # Audit 2026-09-06 (E4, P2): wire the smooth-transition knob (was a
        # dead field — no construction path fed it). Default OFF (inert).
        smooth_cfg = dsl.get("smooth_transition", {}) or {}
        # Audit 2026-09-06 (E3, P2): time-based scratch exit. Default OFF.
        scratch_cfg = dsl.get("time_scratch", {}) or {}
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
            # A-F5: default 4.0s breach confirmation (was 0.0 = single-tick exit).
            breach_confirm_sec=float(dsl.get("breach_confirm_sec", 4.0) or 0.0),
            # H-5: hard max_loss stop wick-guard window (default 1.0s).
            hard_stop_confirm_sec=float(dsl.get("hard_stop_confirm_sec", 1.0) or 0.0),
            noise_band_enabled=bool(noise_cfg.get("enabled", False)),
            noise_band_atr_mult=float(noise_cfg.get("atr_mult", ExitPolicy.noise_band_atr_mult)),
            # Audit 2026-09-06 (E4, P2): smooth phase1→phase2 ramp (default OFF).
            smooth_transition_enabled=bool(smooth_cfg.get("enabled", False)),
            smooth_band_pct=float(smooth_cfg.get("band_pct", ExitPolicy.smooth_band_pct)),
            # Audit 2026-09-06 (E3, P2): time scratch exit (default OFF).
            time_scratch_enabled=bool(scratch_cfg.get("enabled", False)),
            time_scratch_minutes=float(scratch_cfg.get("minutes", ExitPolicy.time_scratch_minutes)),
            time_scratch_min_peak_pct=float(scratch_cfg.get("min_peak_pct", ExitPolicy.time_scratch_min_peak_pct)),
            time_scratch_giveback_pct=float(scratch_cfg.get("giveback_pct", ExitPolicy.time_scratch_giveback_pct)),
            phase2_tiers=tiers if tiers else ExitPolicy().phase2_tiers,
        )
    except Exception as _dp_e:
        logger.error("[dsl] exit-policy build failed, failing open to "
                     "ExitPolicy defaults: %r", _dp_e)
        # Fail-open silently swaps every live stop for the ExitPolicy defaults;
        # mirror loudly so the authoritative feed records the parameter swap.
        try:
            from hermes_trader import event_log
            event_log.append("error", payload={
                "scope": "dsl_policy_build_failopen",
                "error": repr(_dp_e)})
        except Exception as _ev_e:
            logger.error("[dsl] dsl_policy_build_failopen event log failed: %r", _ev_e)
        return ExitPolicy()


def _regime_aware_policy_for(regime: str = "") -> ExitPolicy:
    """Build the SAME regime-aware ExitPolicy a fresh live entry would get, so
    a SYNTHESIZED tracker (post-blackout / orphan rehydrate) inherits the
    regime-specific stops instead of the regime-BLIND ``_policy_from_config()``
    (top-level max_loss_pct / max_loss_roe_pct).

    The live entry path (executor.register) calls ``select_exit_params(dsl,
    regime)`` and builds the policy from those params: trend regimes (up/down)
    get the trend-ride 0.8% / 10%-ROE cap, non-trend (neutral/chop) the scalp
    0.4% / 5%-ROE cap. We start from the full config policy (every other knob)
    and overlay exactly those regime params. Fail-open: any resolution error
    (missing regime_aware block, import error, select failure) returns the base
    config policy — never a crash, never the bare ExitPolicy() default.
    """
    base = _policy_from_config()
    try:
        import dataclasses
        from hermes_trader.agents.config_store import read_agent_config
        from hermes_trader.agents.executor import select_exit_params, resolve_regime_clocks
        dsl = read_agent_config().get("dsl_exit", {}) or {}
        _prot, _retrace, _tiers_raw, _ml_pct, _ml_roe, _label = \
            select_exit_params(dsl, regime or "neutral")
        _tiers = [RetraceTier(**t) for t in _tiers_raw] if _tiers_raw else None
        # Audit 2026-09-06 (E3, P2): regime-split lifetime clocks. Returns the
        # global clock when regime_aware.clocks is disabled (default → inert).
        _clocks = resolve_regime_clocks(dsl, regime or "neutral")
        return dataclasses.replace(
            base,
            max_loss_pct=float(_ml_pct),
            max_loss_roe_pct=float(_ml_roe),
            protect_pct=float(_prot),
            retrace_threshold=float(_retrace),
            hard_timeout_minutes=float(_clocks["hard_timeout_minutes"]),
            stale_flat_timeout_minutes=float(_clocks["stale_flat_timeout_minutes"]),
            phase2_tiers=_tiers if _tiers else ExitPolicy().phase2_tiers,
        )
    except Exception as e:
        logger.warning(
            f"[dsl] regime-aware synth policy build failed (regime={regime!r}); "
            f"falling back to base config policy: {e}")
        return base


def rehydrate_from_exchange(asset_positions: Iterable[dict[str, Any]],
                            policy: Optional[ExitPolicy] = None,
                            default_leverage: int = 1,
                            queried_dexes: Optional[set] = None,
                            user: Optional[str] = None) -> list["DSLTracker"]:
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
    dropped: list["DSLTracker"] = []
    # Best-effort live marks derived from the position snapshot itself, so the
    # bracket backfill below can tell a trailing SL that crossed entry from a TP
    # WITHOUT reordering the caller's mids fetch (mids arrive after rehydrate).
    # Direct notional first; fall back to entry + uPnL/szi. A coin missing both
    # simply gets no mark and the backfill uses the entry boundary.
    live_marks: dict[str, float] = {}
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
        mark: Optional[float] = None
        try:
            pv = pos.get("positionValue")
            if pv is not None:
                mark = abs(float(pv)) / abs(szi)
        except (TypeError, ValueError):
            mark = None
        if not mark:
            try:
                up = pos.get("unrealizedPnl")
                if up is not None:
                    # uPnL = (mark - entry) * szi  (szi signed)
                    mark = entry + float(up) / szi
            except (TypeError, ValueError):
                mark = None
        if mark and mark > 0:
            live_marks[coin] = float(mark)
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
            # the default silently widened live stops ("policy drift").
            #
            # Regime parity: when the caller didn't pass an explicit policy,
            # build the SAME regime-aware policy a fresh live entry would get
            # (trend up/down → 0.8%/10%ROE trend-ride; neutral/chop →
            # 0.4%/5%ROE scalp). The position's true entry regime is gone after
            # a state wipe, so we use the CURRENT regime (TTL-cached) as the best
            # available proxy — and it is already what the live entry path used
            # for this coin's most recent gate evaluation. detect_regime fails
            # open to "neutral"; the policy builder fails open to the base
            # config policy, so regime I/O can never block rehydrate.
            synth_regime = ""
            if policy is None:
                try:
                    from hermes_trader.agents.market_regime import detect_regime
                    synth_regime = str(detect_regime(coin) or "neutral")
                except Exception as _rg_e:
                    logger.debug(
                        f"[dsl] synth regime lookup failed for {key} "
                        f"(non-fatal, neutral/scalp): {_rg_e}")
                    synth_regime = "neutral"
                synth_policy = _regime_aware_policy_for(synth_regime)
            else:
                synth_policy = policy
            # Try to resolve the actual fill time so the hard_timeout is
            # accurate. Match on coin/side/price/size so a prior round-trip
            # on the same coin isn't mis-attributed as the current entry.
            _entry_time = _resolve_fill_time_ms(
                user, coin, side, entry_px=entry, size=abs(szi)
            ) if user else None
            if _entry_time is None:
                # Audit 2026-09-06 (E5, P2): resolution failed (API
                # timeout/rate-limit) OR no user was supplied. Falling back to
                # now() would RESET the hard_timeout clock for an old zombie
                # position being re-synthesized after a state wipe (it could
                # then sit for another full hard_timeout window past its real
                # age). Fail-closed instead: age the synth tracker as if it
                # had ALREADY reached the hard_timeout window (plus a small
                # margin), so check() treats it as an exit candidate on the
                # next tick rather than granting fresh time. The conservative
                # exit is the safe direction for a position of unknown age.
                _conservative_age_sec = synth_policy.hard_timeout_minutes * 60.0 + 60.0
                _entry_time = time.time() - _conservative_age_sec
                if user:
                    logger.error(
                        f"[dsl] fill-time resolution FAILED for {key} @ {entry}; "
                        f"synthesizing with a CONSERVATIVE entry time "
                        f"(~{synth_policy.hard_timeout_minutes:.0f} min in the "
                        f"past) so the hard_timeout clock is NOT reset — position "
                        f"is treated as stale/exit-eligible pending verification."
                    )
            _active_positions[key] = DSLTracker(
                coin, side, entry, _entry_time, synth_policy,
                leverage=lev, entry_regime=synth_regime if policy is None else "")
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
                import uuid as _uuid

                from hermes_trader.agents.memory import memory as _mem
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
        _refresh_positions_gauge()

    # Best-effort: fill in any missing exchange bracket oids (e.g. after a restart
    # from a v1 state file, or a just-synthesized tracker). Skips the network call
    # when every tracker already has an sl_oid.
    if user:
        try:
            backfill_brackets_from_exchange(user, marks=live_marks)
        except Exception as e:
            # A failed backfill leaves trackers without exchange bracket oids
            # (cancel/move paths blind); debug-only hid it. Mirror loudly.
            logger.error(f"[dsl] bracket backfill failed (non-fatal): {e}")
            try:
                from hermes_trader import event_log
                event_log.append("error", payload={
                    "scope": "dsl_bracket_backfill_fail",
                    "error": repr(e)})
            except Exception as _ev_e:
                logger.error("[dsl] dsl_bracket_backfill_fail event log failed: %r", _ev_e)

    return dropped


# C-M1 (deep audit 2026-08-28): per-coin throttle for the missing-mid alarm.
# A held position without a usable mark price means the DSL engine CANNOT
# evaluate its exit this tick — previously this was a silent `continue`
# (fail-open: the position was unmonitored with no trace). The exchange-side
# backup SL remains the hard backstop, but the blind tick must be loud.
_MISSING_MID_WARN_INTERVAL_S = 60.0
_last_missing_mid_warn: dict[str, float] = {}

# A-F5 (deep audit 2026-08-28): short-TTL cache for oracle/index prices used
# in floor-breach wick confirmation. Keyed by dex name ("" = main perp dex).
# One metaAndAssetCtxs call per dex per exit pass is enough — the oracle moves
# slowly and only needs to confirm a breach, not to set the floor.
_IDX_CACHE_TTL_S = 5.0
_IDX_CACHE: dict[str, tuple[float, dict[str, float]]] = {}


def _valid_mid(v: Any) -> bool:
    """True when `v` is a usable, positive, finite mark price."""
    try:
        f = float(v)
    except (ValueError, TypeError):
        return False
    return f > 0.0 and math.isfinite(f)


def held_coins_missing_mids(mids: dict[str, Any]) -> list[str]:
    """Coins with an active DSL tracker whose mark price is absent/unusable.

    Used by the trading loop as a feed-health gate: when the price feed is
    dead (empty snapshot) or blind to any held coin, NEW-ENTRY decisions are
    paused for the cycle (exit monitoring still runs; exchange SLs backstop
    positions that cannot be DSL-evaluated). Returns a sorted list.
    """
    return sorted({t.coin for t in _active_positions.values()
                   if not _valid_mid(mids.get(t.coin))})


def get_index_prices(coins: set[str]) -> dict[str, float]:
    """Fresh oracle/index prices ({coin: px}) for the given coins.

    A-F5 (deep audit 2026-08-28): floor-breach exits must be confirmed against
    the INDEX price, not just the instantaneous mid (a wick moves the mid but
    not the oracle blend). Uses metaAndAssetCtxs (weight 20, same call the
    universe loader uses) with a SHORT-TTL in-memory cache so one exit pass
    costs at most one HTTP request per active dex. Any failure returns a
    partial/empty dict — callers degrade to mid-only.
    """
    out: dict[str, float] = {}
    if not coins:
        return out
    now = time.monotonic()
    # Split native vs HIP-3 (HIP-3 coins look like "<dex>:<SYMBOL>").
    dexes: set[str] = {""}
    for c in coins:
        if ":" in c:
            dexes.add(c.split(":", 1)[0])
    try:
        from hermes_trader.client.hl_client import _http_post
    except Exception:
        return out
    for dex in dexes:
        cache_key = dex or ""
        hit = _IDX_CACHE.get(cache_key)
        if hit is not None and (now - hit[0]) < _IDX_CACHE_TTL_S:
            ctx_map = hit[1]
        else:
            try:
                payload: dict[str, Any] = {"type": "metaAndAssetCtxs"}
                if dex:
                    payload["dex"] = dex
                data = _http_post("/info", payload, timeout=8)
                if not (data and isinstance(data, list) and len(data) >= 2):
                    continue
                meta, ctx = data[0], data[1]
                ctx_map = {}
                for i, u in enumerate(meta.get("universe", []) or []):
                    name = u.get("name")
                    if name and i < len(ctx):
                        try:
                            ctx_map[name] = float(ctx[i].get("oraclePx") or 0.0)
                        except (TypeError, ValueError):
                            continue
                _IDX_CACHE[cache_key] = (now, ctx_map)
            except Exception as e:
                logger.warning(f"[dsl] A-F5 index fetch failed for dex={dex or 'main'}: {e}")
                continue
        for c in coins:
            if dex and not c.startswith(f"{dex}:"):
                continue
            if not dex and ":" in c:
                continue
            bare = c.split(":", 1)[1] if dex else c
            px = ctx_map.get(bare) or 0.0
            if px > 0:
                out[c] = px
    return out


def check_all_positions(mids: dict[str, float], index_prices: Optional[dict[str, float]] = None) -> list[ExitVerdict]:
    """Check all active positions against current mids. Call each scan tick.

    Returns list of ExitVerdict for positions that should be closed.

    A-F5: ``index_prices`` maps coin -> oracle/index price for wick cross-check
    of floor breaches; fetched via :func:`get_index_prices` by the loop when
    not supplied. C-M1: a tracker with no usable mark price is skipped (no
    price → no defensible market close; the exchange-side backup SL is the
    backstop), but the blind tick is logged at ERROR level, throttled per
    coin, instead of being silently swallowed.

    Phase 3 alt: when the persistent allMids WebSocket is fresh
    (data_age < 2s), we substitute the scan-snapshot mid for each held
    coin with the WS's real-time mid. This shaves up to scan_interval
    (~15s) off the floor_breach blind window on fast crashes WITHOUT
    changing the main loop cadence or introducing condition variables.
    Falls back to scan mids when WS isn't fresh, isn't connected, or
    doesn't have a particular coin (e.g. HIP-3 namespaced coins — the
    WS allMids channel only carries native HL perp names). The main
    loop's A-F14 feed-freshness gate still runs on the scan mids, so a
    degraded WS never overrides a halt decision.
    """
    exits = []
    now = time.monotonic()
    _idx = index_prices
    if _idx is None:
        try:
            _idx = get_index_prices({t.coin for t in _active_positions.values()})
        except Exception as _e:
            logger.warning(f"[dsl] A-F5 index-price fetch failed (degrading to mid-only): {_e}")
            _idx = {}

    # Phase 3 alt: probe the persistent WS for a fresher mid than the
    # scan snapshot. Best-effort — any failure (WS not started, stale,
    # coin missing) silently falls back to the scan mids, preserving
    # the original behavior. Local import avoids a top-level cycle.
    _ws = None
    _ws_fresh = False
    try:
        from hermes_trader.client.hl_client import _get_ws_mids_instance
        _ws = _get_ws_mids_instance()
        if _ws is not None and _ws.is_connected():
            _ws_fresh = _ws.get_data_age_seconds() < 2.0
    except Exception:
        _ws = None
    _ws_substituted = 0

    for tracker in list(_active_positions.values()):
        # Phase 3 alt: prefer the WS real-time mid over the scan-snapshot
        # mid for the floor_breach decision. Falls back to scan mids
        # when WS isn't fresh or doesn't carry this coin (HIP-3 namespaced
        # coins aren't on the native allMids channel, so they naturally
        # take the fallback path here).
        mark_px = None
        if _ws is not None and _ws_fresh:
            try:
                _ws_px = _ws.get_price(tracker.coin)
                if _ws_px > 0.0 and math.isfinite(_ws_px):
                    mark_px = _ws_px
                    _ws_substituted += 1
            except Exception:
                pass
        if mark_px is None:
            mark_px = mids.get(tracker.coin)
        # Handle both str and float values from different sources
        if mark_px is not None:
            try:
                mark_px = float(mark_px)
            except (ValueError, TypeError):
                mark_px = None
        if mark_px is None or not (mark_px > 0.0 and math.isfinite(mark_px)):
            last = _last_missing_mid_warn.get(tracker.coin, 0.0)
            if now - last >= _MISSING_MID_WARN_INTERVAL_S:
                _last_missing_mid_warn[tracker.coin] = now
                logger.error(
                    f"[dsl] NO USABLE MID for held {tracker.coin} {tracker.side} "
                    f"(raw={mark_px!r}) — DSL exit NOT evaluated this tick; "
                    f"exchange backup SL is the only stop. Feed failure must "
                    f"pause new entries (FEED-FRESHNESS).")
            continue
        verdict = tracker.check(mark_px, index_px=(_idx or {}).get(tracker.coin))
        if verdict.exit:
            exits.append(verdict)

    # Phase 3 alt: log substitution stats so ops can verify the path is
    # exercised. Only logs when SOME but not ALL positions fell back to
    # scan mids (a partial substitution means WS missed coins that the
    # operator might want to know about — e.g. a HIP-3 position holding
    # alongside native perps). All-or-nothing paths (full substitution
    # or zero substitution) stay silent to avoid log spam at scan cadence.
    if _ws is not None and _ws_fresh:
        try:
            _total = len(_active_positions)
            if 0 < _ws_substituted < _total:
                logger.info(
                    "[dsl:ws-mid] %d/%d positions used WS real-time mid; "
                    "rest fell back to scan mids (HIP-3 or missing)",
                    _ws_substituted, _total,
                )
        except Exception:
            pass

    return exits
