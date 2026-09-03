"""Cross-process positions snapshot.

The trading loop fetches the full account state (`fetch_account_state`,
~9 HTTP POSTs across the main + HIP-3 clearinghouses) every cycle. The web
dashboard used to fetch the SAME state independently on every poll — two
processes sharing one IP, neither's rate-limiter aware of the other, which
collectively tripped Hyperliquid's per-IP weight limit (429s + read timeouts).

The loop already paid for that fetch, so it now writes the raw position list
to a small snapshot file each cycle. The dashboard reads the snapshot instead
of calling HL, making it a pure file reader for the positions view. Only the
loop talks to HL → the cross-process contention is gone. The snapshot is at
most one loop-cycle stale (~60s), which is invisible for a bot that holds
positions for hours.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from hermes_trader.agents import atomic_io
from hermes_trader.contracts import parse_snapshot

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT_FILE = os.environ.get(
    "HERMES_POSITIONS_SNAPSHOT_FILE",
    os.path.join(_REPO_ROOT, ".positions-snapshot.json"),
)

# P0-2c: payload schema version. Files written before this field existed have
# no ``version`` and are treated as v0 (identical layout: saved_at +
# asset_positions). A version newer than this binary is rejected on read so a
# downgraded daemon never mis-parses a newer schema — the caller then falls
# back to a live fetch.
_SNAPSHOT_VERSION = 1


def write_snapshot(asset_positions: list[dict[str, Any]]) -> None:
    """Atomically persist the raw HL position list. Best-effort, never raises."""
    try:
        payload = {
            "version": _SNAPSHOT_VERSION,
            "saved_at": int(time.time() * 1000),
            "asset_positions": asset_positions or [],
        }
        # Regenerable every loop cycle: atomic rename only (no torn reads),
        # but fsync=False — agents.atomic_io owns the tmp+replace machinery.
        atomic_io.write_json_atomic(SNAPSHOT_FILE, payload, indent=None, fsync=False)
    except OSError as e:
        logger.warning(f"[snapshot] failed to persist positions: {e}")


def read_snapshot(max_age_s: float = 120.0) -> Optional[dict[str, Any]]:
    """Return a state-like dict ({"asset_positions": [...]}) from the snapshot,
    or None if the file is missing, unreadable, or older than `max_age_s`.

    A None return signals the caller to fall back to a live fetch — e.g. when
    the loop isn't running, so a standalone dashboard still shows positions.
    """
    try:
        with open(SNAPSHOT_FILE) as f:
            payload = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[snapshot] file unreadable, ignoring: {e}")
        return None

    # P0-2d: schema validation at the read boundary. parse_snapshot rejects a
    # non-object payload / mistyped scalars / a non-list asset_positions
    # (→ None → live fetch fallback) and skips individual malformed rows
    # instead of letting them poison downstream readers.
    parsed = parse_snapshot(payload)
    if parsed is None:
        return None
    # P0-2c: version gate. Missing version = v0 legacy file (same layout);
    # a future version this binary doesn't understand → None so the caller
    # re-fetches live instead of mis-parsing.
    version = parsed["version"]
    if version > _SNAPSHOT_VERSION:
        logger.warning(
            f"[snapshot] file version {version} newer than this binary "
            f"(expects v{_SNAPSHOT_VERSION}); ignoring — live fetch fallback"
        )
        return None

    saved_at = parsed["saved_at"]
    age_s = (time.time() * 1000 - saved_at) / 1000.0
    if age_s > max_age_s:
        return None
    return {"asset_positions": parsed["asset_positions"]}
