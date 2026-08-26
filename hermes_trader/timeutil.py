"""Unified UTC time utilities — project-local shim.

Forwards to the single source of truth at ``~/.hermes-trading/timeutil.py``
when available; otherwise falls back to an inline copy.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, date
from typing import Union

_SHARED = os.path.expanduser("~/.hermes-trading")
if _SHARED not in sys.path and os.path.isfile(os.path.join(_SHARED, "timeutil.py")):
    sys.path.insert(0, _SHARED)
    from timeutil import (  # type: ignore  # noqa: F401
        utcnow,
        utcnow_iso,
        epoch_s,
        epoch_ms,
        to_iso_z,
        parse_iso,
        today_utc,
        today_utc_str,
        date_to_iso,
    )
else:  # pragma: no cover - inline fallback

    def utcnow() -> datetime:
        return datetime.now(timezone.utc)

    def utcnow_iso() -> str:
        return to_iso_z(utcnow())

    def epoch_s() -> float:
        return utcnow().timestamp()

    def epoch_ms() -> int:
        return int(utcnow().timestamp() * 1000)

    def to_iso_z(value: Union[datetime, int, float, None]) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, (int, float)):
            if value >= 1e12:
                dt = datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
            else:
                dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            raise TypeError(f"unsupported time value: {value!r}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def parse_iso(value: str) -> datetime:
        if not value:
            raise ValueError("empty timestamp")
        v = value.strip()
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def today_utc() -> date:
        return utcnow().date()

    def today_utc_str() -> str:
        return today_utc().isoformat()

    def date_to_iso(d: date) -> str:
        return d.isoformat()
