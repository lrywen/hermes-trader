"""Selection -> forward-outcome attribution for the ranking/selection arms.

WHY
---
``signal_ranking`` and ``coin_selection`` run in SHADOW and only record *what
they picked*. Without linking each pick/defer to the coin's subsequent return
there is no way to tell whether the selection actually raises expectancy, so
the arm can neither be promoted nor refuted — it is dead weight (the exact
failure Pathiel's "no recorders" directive targets).

This module closes that loop:

  * ``register_pending`` — when a shadow selection runs, log every chosen and
    every deferred coin once with its base price, bucket, score and a horizon.
  * ``settle_due``       — once an item's horizon has elapsed, fetch the forward
    1h closes and write the forward return (vs the registered base price) back.
  * ``attribution_summary`` — mean forward return of selected vs deferred, with
    a same-cycle paired permutation test for significance.

Forward returns use the MAIN-market 1h ``candleSnapshot`` on the bare coin
(stripped of any ``dex:`` prefix). That is the right instrument for "does the
selection predict the next move" and keeps the arm dex-agnostic; HIP-3 vs main
execution differences are out of scope here.

Storage is a single JSON state file (pending + settled), replaced atomically
(.tmp + os.replace) and guarded by a process lock. Never raises into the caller:
attribution must never perturb the trade hot path.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

from hermes_trader.agents import atomic_io

logger = logging.getLogger(__name__)

_WRITE_LOCK = threading.Lock()

# Default settle horizon (hours). Promotion gates read the summary at this.
DEFAULT_HORIZON_HOURS = 72

# Attribution settles on 1h closes, so the logical "cycle" for one
# observation is a 1h bucket — not an individual scan tick. Both call sites
# used to pass a raw wall-clock ms (one per scan), which made the
# (source, coin, cycle) dedup key unique every tick and re-registered the
# whole coin universe every ~3-5 min (observed 2026-10-15: items/cycle jumped
# ~2 -> ~25 and daily registrations rose ~15x to 8-12k, an unbounded backlog).
# Normalising the cycle onto the 1h grid makes repeated scans inside the same
# hour collapse to one observation (backlog fix, recheck 2026-10-18).
CYCLE_BUCKET_MS = 3_600_000

# Wall-clock ms above this is treated as a real epoch timestamp and bucketed;
# tiny synthetic values (tests use 1_000) are left untouched. 10^11 ms is
# 1973-03, far below any real run and far above any hand-written fixture.
_WALLCLOCK_MIN_MS = 100_000_000_000


def _logical_cycle_ms(cycle_ms: int) -> int:
    """Snap a raw cycle timestamp onto the 1h attribution grid.

    Small synthetic values (< _WALLCLOCK_MIN_MS) are returned unchanged so
    callers/tests that pass an explicit logical cycle id keep full control.
    """
    if cycle_ms >= _WALLCLOCK_MIN_MS:
        return (cycle_ms // CYCLE_BUCKET_MS) * CYCLE_BUCKET_MS
    return cycle_ms

# Per-file caps so the settled history can't grow unbounded. Settled rows are
# small; rotation here is by-count (oldest dropped), matching the shadow arms'
# conservative retention.
MAX_SETTLED_PER_SOURCE = 4000


def _data_dir() -> str:
    raw = os.environ.get("HERMES_DATA_DIR")
    return raw if raw else "/data"


def _state_path() -> str:
    return os.path.join(_data_dir(), "selection_attribution.json")


def _now_ms() -> int:
    return int(time.time() * 1000)


def bare_coin(coin: str) -> str:
    """Strip a ``dex:`` prefix (e.g. ``xyz:IREN`` -> ``IREN``)."""
    return coin.split(":", 1)[1] if isinstance(coin, str) and ":" in coin else coin


def _load_state() -> dict[str, Any]:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (FileNotFoundError, ValueError):
        pass
    return {"items": []}


def _save_state(state: dict[str, Any]) -> None:
    # P2 (audit 2026-10-15): settle attribution is authoritative decision
    # state (fwd_pct written back onto pending items); a lost write corrupts
    # attribution. Migrated off the hand-rolled tmp+os.replace (no fsync) to
    # the shared locked atomic writer for file+dir fsync + cross-process flock.
    atomic_io.locked_write_json_atomic(
        _state_path(), state, fsync=True, ebusy_fallback=True,
        indent=None, default=str)


def _dedup_key(source: str, coin: str, cycle_ms: int) -> str:
    return f"{source}|{coin}|{cycle_ms}"


def register_pending(
    source: str,
    selections: list[tuple[str, Optional[float]]],
    deferrals: list[tuple[str, Optional[float]]],
    *,
    base_prices: dict[str, Any],
    cycle_ms: Optional[int] = None,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
) -> int:
    """Register one cycle's chosen + deferred coins for forward attribution.

    ``selections``/``deferrals`` are ``(coin, score)`` pairs. ``base_prices``
    maps coin -> price (mids); coins with no usable base price are skipped (a
    return can't be measured without one). Idempotent per
    (source, coin, cycle): re-registering the same cycle adds nothing. A
    real-epoch ``cycle_ms`` is snapped onto the 1h attribution grid, so several
    scans within the same hour collapse to one observation; a small synthetic
    cycle id is used verbatim.
    Returns the number of items added. Never raises.
    """
    try:
        ts = _now_ms()
        raw_cycle = int(cycle_ms) if cycle_ms else ts
        # Snap onto the 1h grid so several scans inside the same hour are one
        # observation (they settle against the same +72h 1h close). Without
        # this the raw per-scan timestamp defeats the dedup key (recheck P0-D
        # root cause, 2026-10-18).
        cycle = _logical_cycle_ms(raw_cycle)
        with _WRITE_LOCK:
            state = _load_state()
            seen = {_dedup_key(it.get("source", ""), it.get("coin", ""),
                               it.get("cycle", 0))
                    for it in state["items"]}
            added = 0
            for bucket, rows in (("selected", selections),
                                 ("deferred", deferrals)):
                for coin, score in rows:
                    key = _dedup_key(source, coin, cycle)
                    if key in seen:
                        continue
                    bc = bare_coin(coin)
                    raw_px = base_prices.get(bc) if base_prices else None
                    try:
                        base_px = float(raw_px)
                    except (TypeError, ValueError):
                        continue
                    if base_px <= 0:
                        continue
                    state["items"].append({
                        "source": source,
                        "coin": coin,
                        "cycle": cycle,
                        "registered_ms": ts,
                        "bucket": bucket,
                        "score": (None if score is None
                                  else round(float(score), 4)),
                        "base_px": base_px,
                        "horizon_hours": int(horizon_hours),
                        "due_ms": ts + int(horizon_hours) * 3600_000,
                        "fwd_pct": None,
                        "settled_ms": None,
                    })
                    seen.add(key)
                    added += 1
            _cap_settled(state)
            if added:
                _save_state(state)
        return added
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[selection_attribution] register failed: %s", e)
        return 0


def _forward_close(coin: str, start_ms: int, horizon_hours: int) -> Optional[float]:
    """Close of the 1h bar ``horizon_hours`` after ``start_ms`` (main market)."""
    from hermes_trader.client.hl_client import _http_post

    step = 3600_000
    grid0 = start_ms - (start_ms % step)
    end = grid0 + (horizon_hours + 3) * step
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1h",
        "startTime": grid0 + (horizon_hours - 1) * step,
        "endTime": end}}
    # Observability caller: pass a short max_wait so this synchronous settle
    # yields the rate budget to the trading path instead of parking the main
    # thread on the limiter for rate_max_wait_s (30s). A miss is harmless — the
    # item stays pending and is retried next sweep.
    raw = _http_post("/info", payload, max_wait=2.0)
    if not isinstance(raw, list) or not raw:
        return None
    target_t = grid0 + horizon_hours * step
    closes = {int(c["t"]): float(c["c"]) for c in raw}
    return closes.get(target_t)


def settle_due(*, now_ms: Optional[int] = None,
               max_per_sweep: Optional[int] = None) -> int:
    """Settle every pending item whose horizon elapsed. Returns settled count.

    A coin whose forward bar hasn't printed (API gap / still too young) is left
    pending and retried next sweep. Never raises.

    ``max_per_sweep`` caps how many items are settled in a single sweep. The
    main loop passes a small budget so a large backlog can never stall the
    trade hot path on the HL rate limiter: each settle fires a synchronous
    1h candleSnapshot HTTP POST, and with ``rate_max_wait_s`` (30s) behind a
    just-exhausted scan budget, an unbounded backlog parks the main thread
    past the 600s watchdog and triggers a re-exec loop (observed 2026-10-14).
    Default ``None`` = no cap (kept for the CLI / reconcile script / tests).
    """
    try:
        now = now_ms if now_ms is not None else _now_ms()
        settled = 0
        with _WRITE_LOCK:
            state = _load_state()
            changed = False
            for it in state["items"]:
                if it.get("fwd_pct") is not None:
                    continue
                if now < int(it.get("due_ms", 0)):
                    continue
                if max_per_sweep is not None and settled >= max_per_sweep:
                    break
                px = _forward_close(bare_coin(it["coin"]),
                                    int(it["registered_ms"]),
                                    int(it["horizon_hours"]))
                if px is None:
                    continue
                base = float(it["base_px"])
                it["fwd_pct"] = round((px - base) / base * 100.0, 4)
                it["settled_ms"] = now
                settled += 1
                changed = True
            if changed:
                _cap_settled(state)
                _save_state(state)
        return settled
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[selection_attribution] settle failed: %s", e)
        return 0


def _cap_settled(state: dict[str, Any]) -> None:
    """Drop oldest settled rows per source (pending rows always kept)."""
    items = state["items"]
    pending = [it for it in items if it.get("fwd_pct") is None]
    settled = [it for it in items if it.get("fwd_pct") is not None]
    if len(settled) <= MAX_SETTLED_PER_SOURCE:
        return
    by_source: dict[str, list[dict[str, Any]]] = {}
    for it in settled:
        by_source.setdefault(it.get("source", ""), []).append(it)
    kept: list[dict[str, Any]] = []
    for src, rows in by_source.items():
        rows.sort(key=lambda r: r.get("settled_ms", 0))
        if len(rows) > MAX_SETTLED_PER_SOURCE:
            rows = rows[-MAX_SETTLED_PER_SOURCE:]
        kept.extend(rows)
    kept.sort(key=lambda r: r.get("settled_ms", 0))
    state["items"] = pending + kept


def attribution_summary(
    source: str,
    *,
    horizon_hours: Optional[int] = None,
) -> dict[str, Any]:
    """Selected vs deferred forward-return stats for one source.

    Returns counts/means for both buckets, the mean difference, and a paired
    same-cycle permutation p-value (cycles that contain both a selected and a
    deferred coin, compared within cycle). Empty buckets / no pairs yield
    ``p_value=None``. Pure read; never raises.
    """
    empty = {"source": source, "n_selected": 0, "n_deferred": 0,
             "mean_selected_pct": None, "mean_deferred_pct": None,
             "mean_diff_pct": None, "n_pairs": 0, "p_value": None}
    try:
        state = _load_state()
        rows = [it for it in state["items"] if it.get("source") == source
                and it.get("fwd_pct") is not None]
        if horizon_hours is not None:
            rows = [it for it in rows
                    if int(it.get("horizon_hours", 0)) == int(horizon_hours)]
        sel = [float(it["fwd_pct"]) for it in rows
               if it.get("bucket") == "selected"]
        defer = [float(it["fwd_pct"]) for it in rows
                 if it.get("bucket") == "deferred"]
        if not sel and not defer:
            return empty

        def _mean(xs: list[float]) -> Optional[float]:
            return round(sum(xs) / len(xs), 4) if xs else None

        ms, md = _mean(sel), _mean(defer)
        diff = round(ms - md, 4) if ms is not None and md is not None else None

        # Pair within cycle: mean(selected) - mean(deferred) per cycle.
        by_cycle: dict[int, dict[str, list[float]]] = {}
        for it in rows:
            c = by_cycle.setdefault(int(it["cycle"]), {"s": [], "d": []})
            c["s" if it.get("bucket") == "selected" else "d"].append(
                float(it["fwd_pct"]))
        obs_diffs = []
        for c in by_cycle.values():
            if c["s"] and c["d"]:
                obs_diffs.append(sum(c["s"]) / len(c["s"])
                                 - sum(c["d"]) / len(c["d"]))
        p_value = _paired_perm_p(obs_diffs) if obs_diffs else None
        return {
            "source": source,
            "n_selected": len(sel),
            "n_deferred": len(defer),
            "mean_selected_pct": ms,
            "mean_deferred_pct": md,
            "mean_diff_pct": diff,
            "n_pairs": len(obs_diffs),
            "p_value": p_value,
        }
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[selection_attribution] summary failed: %s", e)
        return empty


def _paired_perm_p(obs_diffs: list[float], *, draws: int = 10_000,
                   seed: int = 12345) -> Optional[float]:
    """Two-sided sign-flip permutation p for paired within-cycle diffs."""
    import random

    n = len(obs_diffs)
    if n == 0:
        return None
    observed = abs(sum(obs_diffs))
    rng = random.Random(seed)
    ge = 0
    for _ in range(draws):
        total = 0.0
        for d in obs_diffs:
            total += d if rng.random() < 0.5 else -d
        if abs(total) >= observed:
            ge += 1
    return round((ge + 1) / (draws + 1), 5)
