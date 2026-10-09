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

logger = logging.getLogger(__name__)

_WRITE_LOCK = threading.Lock()

# Default settle horizon (hours). Promotion gates read the summary at this.
DEFAULT_HORIZON_HOURS = 72

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
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


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
    (source, coin, cycle): re-registering the same cycle adds nothing.
    Returns the number of items added. Never raises.
    """
    try:
        ts = _now_ms()
        cycle = int(cycle_ms) if cycle_ms else ts
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
    raw = _http_post("/info", payload)
    if not isinstance(raw, list) or not raw:
        return None
    target_t = grid0 + horizon_hours * step
    closes = {int(c["t"]): float(c["c"]) for c in raw}
    return closes.get(target_t)


def settle_due(*, now_ms: Optional[int] = None) -> int:
    """Settle every pending item whose horizon elapsed. Returns settled count.

    A coin whose forward bar hasn't printed (API gap / still too young) is left
    pending and retried next sweep. Never raises.
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
