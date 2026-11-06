"""Loop-heartbeat contract and its default-filling read parser."""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


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
