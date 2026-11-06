"""Positions-snapshot contract and its fault-tolerant read parser."""
from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


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
