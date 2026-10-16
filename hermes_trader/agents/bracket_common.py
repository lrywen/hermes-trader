"""Shared post-fill bracket primitives (audit 2026-10-15 P1, bracket dedup).

The autonomous entry path (``executor``) and the manual web-order path
(``server``) used to each hand-copy two money-safety routines:

  1. backup-SL width validation + clamping (finiteness, hard-max ceiling,
     floor<=ceiling, band<=ceiling), and
  2. the idempotent stop-placement state machine: ONE cloid per intent, claim
     a possibly-resting order by cloid BEFORE any retry on a lost response,
     retry-once-with-new-cloid only when the lookup confirms absence, and
     never blind-retry when the lookup itself is unreadable.

Two copies already drifted historically (the HYPE -252% ROE gap and the
2026-09-08 cloid TypeError). This module holds the single implementation of
the pure/transport-agnostic parts. Process-specific scaffolding stays with the
callers and is INJECTED:

  * executor keeps its deferred ``_pending_sl_retries`` queue, metrics and its
    module-level ``set_bracket``;
  * server keeps ``dsl_exit.load_state(force=True)`` + ``dsl_exit.set_bracket``
    and its synchronous web-process logging.

Nothing here reads global config, the exchange, or memory; the callers supply
the resolved widths, the placement/lookup callables and a ``log`` callable.
"""
from __future__ import annotations

import math
import time
import uuid
from typing import Any, Callable, Optional

from hyperliquid.utils.types import Cloid


def validate_clamped_sl_widths(
    *,
    sl_atr_mult: float,
    sl_ceiling_pct: float,
    sl_floor_pct: float,
    sl_limit_band_pct: float,
    hard_max_pct: float,
    default_atr_mult: float,
    default_ceiling_pct: float,
    default_floor_pct: float,
    default_hard_max_pct: float = 15.0,
    log: Optional[Callable[[str], None]] = None,
    log_prefix: str = "bracket",
) -> dict[str, float]:
    """Validate and clamp backup-stop widths identically for both entry paths.

    Mirrors the post-HYPE rules: every input must be finite/positive (else fall
    back to the supplied default); the ceiling is capped at the hard max; the
    floor is pinned to the ceiling (never invert the clamp); the limit band is
    pinned to the ceiling (a worst-case limit fill must not print past the
    outer-net cap). ``log`` receives a single human-readable message.
    """
    def _warn(msg: str) -> None:
        if log is not None:
            log(msg)

    if not (math.isfinite(sl_atr_mult) and sl_atr_mult > 0):
        _warn(f"{log_prefix} invalid sl_atr_mult={sl_atr_mult} — fallback {default_atr_mult}")
        sl_atr_mult = default_atr_mult
    if not (math.isfinite(sl_ceiling_pct) and sl_ceiling_pct > 0):
        _warn(f"{log_prefix} invalid sl_ceiling_pct={sl_ceiling_pct} — fallback {default_ceiling_pct}")
        sl_ceiling_pct = default_ceiling_pct
    if not (math.isfinite(sl_floor_pct) and sl_floor_pct > 0):
        _warn(f"{log_prefix} invalid sl_floor_pct={sl_floor_pct} — fallback {default_floor_pct}")
        sl_floor_pct = default_floor_pct
    if not (math.isfinite(hard_max_pct) and hard_max_pct > 0):
        _warn(f"{log_prefix} invalid sl_ceiling_hard_max_pct={hard_max_pct} — "
              f"fallback {default_hard_max_pct}")
        hard_max_pct = default_hard_max_pct
    if sl_ceiling_pct > hard_max_pct:
        _warn(f"{log_prefix} sl_ceiling_pct={sl_ceiling_pct}% exceeds hard max "
              f"{hard_max_pct}% — clamping")
        sl_ceiling_pct = hard_max_pct
    if sl_floor_pct > sl_ceiling_pct:
        # floor must never exceed ceiling (would place a stop on the wrong side)
        sl_floor_pct = sl_ceiling_pct
    if sl_limit_band_pct > sl_ceiling_pct:
        _warn(f"{log_prefix} sl_limit_band_pct={sl_limit_band_pct}% exceeds ceiling "
              f"{sl_ceiling_pct}% — clamping band to ceiling")
        sl_limit_band_pct = sl_ceiling_pct
    return {
        "sl_atr_mult": float(sl_atr_mult),
        "sl_ceiling_pct": float(sl_ceiling_pct),
        "sl_floor_pct": float(sl_floor_pct),
        "sl_limit_band_pct": float(sl_limit_band_pct),
    }


def new_stop_cloid() -> Cloid:
    """Mint a fresh stop-intent cloid (single helper so both paths agree)."""
    return Cloid.from_int(uuid.uuid4().int)


def place_backup_stop_once(
    *,
    is_buy: bool,
    size_in_coin: float,
    sl_px: float,
    coin: str,
    limit_band_pct: float = 0.0,
    place_order: Callable[..., dict[str, Any]],
    find_by_cloid: Callable[..., dict[str, Any]],
    sleep: Callable[[float], None] = time.sleep,
    log: Optional[Callable[..., None]] = None,
    log_prefix: str = "bracket",
) -> dict[str, Any]:
    """Place the backup exchange stop with the idempotent cloid state machine.

    ONE cloid identifies this SL intent. On a lost placement response
    (``error_code == 'response_unknown'``) the order may actually be resting,
    so it is claimed by cloid BEFORE any retry:

      * found resting      -> treat as armed (adopt the resting oid/price);
      * lookup OK + absent -> mint a NEW cloid and retry exactly once;
      * lookup unreadable  -> preserve the cloid, do NOT blind-retry (the
                              caller reconciles / deferred-rearms with it).

    A normal (non-response_unknown) failure gets a single retry with the SAME
    cloid after ``sleep(2)``.

    Returns ``{ok, order_id, sl_px, cloid, missing}``. ``missing=True`` means
    no confirmed server-side stop rests; the caller owns the alerting /
    deferred re-arm / emergency-flatten decision.
    """
    def _log(level: str, msg: str) -> None:
        if log is not None:
            log(level, msg)

    band_kw: dict[str, Any] = {"limit_band_pct": limit_band_pct} if limit_band_pct > 0 else {}
    cloid = new_stop_cloid()
    band_kw["cloid"] = cloid
    res = place_order(is_buy, size_in_coin, sl_px, "sl", coin, **band_kw)

    skip_retry = False
    if not res.get("ok") and res.get("error_code") == "response_unknown":
        claim = find_by_cloid(coin, cloid)
        if claim.get("found") and claim.get("oid"):
            _log("warning",
                 f"{log_prefix} SL response lost for {coin} but claimed resting "
                 f"order by cloid: oid={claim.get('oid')} trigger={claim.get('trigger_px')}")
            if claim.get("trigger_px"):
                sl_px = float(claim["trigger_px"])
            res = {"ok": True, "order_id": claim["oid"]}
        elif claim.get("lookup_ok"):
            _log("warning",
                 f"{log_prefix} SL response lost for {coin}; cloid confirmed absent "
                 "on exchange — retrying once with a NEW cloid")
            cloid = new_stop_cloid()
            band_kw["cloid"] = cloid
            sleep(2)
            res = place_order(is_buy, size_in_coin, sl_px, "sl", coin, **band_kw)
            skip_retry = True
        else:
            _log("error",
                 f"{log_prefix} SL response lost for {coin} AND openOrders lookup "
                 "failed — preserving cloid, no blind retry")
            skip_retry = True

    if not res.get("ok") and not skip_retry:
        sleep(2)
        res = place_order(is_buy, size_in_coin, sl_px, "sl", coin, **band_kw)

    return {
        "ok": bool(res.get("ok")),
        "order_id": res.get("order_id"),
        "sl_px": sl_px,
        "cloid": cloid,
        "missing": not bool(res.get("ok")),
        "error": res.get("error"),
    }
