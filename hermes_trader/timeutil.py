"""Unified UTC time utilities — project-local shim.

Forwards to the single source of truth at ``~/.hermes-trading/timeutil.py``
when available; otherwise falls back to an inline copy.
"""

from __future__ import annotations

import importlib.util
import os
import stat
from datetime import date, datetime, timezone
from typing import Union

_SHARED = os.path.expanduser("~/.hermes-trading")


def _load_shared_timeutil():
    """M-8 (supplemental audit 2026-08-30): load the shared timeutil by ABSOLUTE
    file path instead of inserting ``~/.hermes-trading`` at sys.path[0].

    The old ``sys.path.insert`` let any ``.py`` dropped in that user-writable
    directory shadow / hijack imports (import-time code execution). We now:

      1. harden the directory to mode 0700 (and require it to be owned by the
         current effective uid — refuse otherwise), and
      2. load ``timeutil.py`` explicitly via importlib from its absolute path.

    Returns the loaded module, or None to use the inline fallback.
    """
    path = os.path.join(_SHARED, "timeutil.py")
    try:
        if not os.path.isfile(path):
            return None
        st = os.stat(_SHARED)
        if st.st_uid != os.geteuid():
            return None  # not ours — never auto-harden/load another uid's dir
        if st.st_mode & 0o077:
            os.chmod(_SHARED, stat.S_IRWXU)  # tighten to 0700
        spec = importlib.util.spec_from_file_location("_hermes_shared_timeutil", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


_shared = _load_shared_timeutil()
if _shared is not None:
    utcnow = _shared.utcnow
    utcnow_iso = _shared.utcnow_iso
    epoch_s = _shared.epoch_s
    epoch_ms = _shared.epoch_ms
    to_iso_z = _shared.to_iso_z
    parse_iso = _shared.parse_iso
    today_utc = _shared.today_utc
    today_utc_str = _shared.today_utc_str
    date_to_iso = _shared.date_to_iso
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
