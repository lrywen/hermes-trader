"""Pydantic data contracts for cross-process read boundaries (P0-2d).

The trading loop, dashboard and snapshot file are separate writers/readers of
JSON payloads that were historically hand-coerced with ad-hoc ``.get(...)`` /
``float(...)`` chains at every call site. A single malformed field (a string
where a number belongs, a dict where a list belongs) either raised into a
read endpoint or silently degraded to a wrong zero. This module centralises
the schemas for those payloads and validates them at the **read boundary**,
mirroring ``research_schema.parse_structured``:

* validation failure never raises — the parse helper returns ``None`` and the
  caller takes the same fallback path it already has for a missing payload;
* unknown/forward fields are ignored so a newer writer never breaks an older
  reader;
* nested list entries are validated row-by-row: one malformed position row is
  skipped with a warning, never allowed to void the whole payload.

This is display/read hardening only. The trading path's own state files
(memory, shadow book, DSL trackers) keep their dedicated loaders; no validated
dict flows back into order/sizing decisions.
"""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


# ── Positions snapshot ───────────────────────────────────────────────────────

class EnvelopePosition(BaseModel):
    """One raw Hyperliquid ``assetPositions`` row: ``{"position": {...}}``.

    Only the fields readers consume are typed; the dozen camelCase exchange
    fields (``positionValue``, ``unrealizedPnl``, ``leverage``, ...) are kept
    verbatim via ``extra="allow"`` so the validated dict is a drop-in for the
    raw row. ``position`` is permissive (dict) on purpose — each downstream
    reader tolerates the sub-fields it needs missing.
    """

    model_config = {"extra": "allow"}

    position: dict[str, Any]


class SnapshotMeta(BaseModel):
    """Envelope for the positions snapshot file written by ``write_snapshot``."""

    model_config = {"extra": "ignore"}

    version: int = 0
    saved_at: int = 0
    asset_positions: list[Any] = Field(default_factory=list)


def parse_snapshot(payload: Any) -> Optional[dict[str, Any]]:
    """Validate a positions-snapshot JSON payload.

    Returns ``{"version", "saved_at", "asset_positions"}`` on success, or
    ``None`` when the payload is not a JSON object, its scalar fields are
    mistyped, or ``asset_positions`` is not a list — the caller then falls
    back to a live account fetch. Individual malformed position rows are
    skipped (warned) rather than voiding the whole snapshot.
    """
    try:
        meta = SnapshotMeta.model_validate(payload)
    except ValidationError as e:
        logger.warning(f"[contracts] positions snapshot rejected: {e}")
        return None

    valid_rows: list[dict[str, Any]] = []
    for idx, row in enumerate(meta.asset_positions):
        try:
            valid_rows.append(EnvelopePosition.model_validate(row).model_dump())
        except ValidationError as e:
            logger.warning(
                f"[contracts] snapshot position row {idx} malformed, skipping: {e}"
            )
    return {
        "version": meta.version,
        "saved_at": meta.saved_at,
        "asset_positions": valid_rows,
    }


# ── Loop heartbeat ───────────────────────────────────────────────────────────

class LoopHeartbeat(BaseModel):
    """Schema for a ``loop_heartbeat`` session-log event.

    All numeric scalar fields default to zero / empty so a heartbeat written
    by an older loop (pre-upgrade events lack ``cum_contrib`` etc.) still
    validates; readers keep doing their own ``or 0`` math on top. ``config``
    and the per-dex breakdowns are left as opaque dicts — their internal keys
    vary and each consumer projects only the few it needs.
    """

    model_config = {"extra": "ignore"}

    event: Literal["loop_heartbeat"] = "loop_heartbeat"
    ts: int = 0
    equity: float = 0.0
    available: float = 0.0
    daily_pnl: float = 0.0
    # P0-1: cumulative net external capital flow (display-only).
    cum_contrib: float = 0.0
    spot_usdc: float = 0.0
    open_positions: int = 0
    dex_equity: dict[str, Any] = Field(default_factory=dict)
    dex_available: dict[str, Any] = Field(default_factory=dict)
    config: Optional[dict[str, Any]] = None


def parse_heartbeat(event: Any) -> Optional[dict[str, Any]]:
    """Validate one session-log event as a ``loop_heartbeat``.

    Returns the validated, default-filled dict (``config`` normalised to an
    empty dict when absent), or ``None`` on validation failure. Never raises.
    """
    try:
        hb = LoopHeartbeat.model_validate(event)
    except ValidationError as e:
        logger.warning(f"[contracts] loop_heartbeat event rejected: {e}")
        return None
    out = hb.model_dump()
    out["config"] = hb.config or {}
    return out
