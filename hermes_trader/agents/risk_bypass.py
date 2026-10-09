"""Operator emergency bypass for frequency-reduction gates only (A5).

A single read-mostly switch, ``risk_bypass_all``, that lets an operator
temporarily unblock the bot during a high-volatility opportunity when several
cooldown/de-rate gates would otherwise stack and keep it out of the market.

HARD SCOPE LIMIT — this may ONLY bypass frequency/cooldown gates:
  * anti-revenge loss cooldown (``_loss_cooldown_block``)
  * adaptive research cooldown
It must NEVER bypass the safety gates: daily-loss kill switch, free-margin /
min-notional / max-leverage gates, idempotency. Those callers do not read this.

Controls:
  * default off;
  * only operator/admin may set it (the BFF write path is fail-closed — the
    switch simply never being settable from an untrusted role is enforced at
    the portal layer);
  * every arm writes an audit record;
  * auto-expires after TTL_SECONDS so it cannot be left on accidentally.

Read is best-effort: any failure returns "not bypassing" (fail-safe). Never
raises into a trading gate.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

# Auto-expiry: the bypass can never silently stay armed for longer than this.
TTL_SECONDS = 30 * 60


def _data_dir() -> str:
    raw = os.environ.get("HERMES_DATA_DIR")
    return raw if raw else "/data"


def _state_path() -> str:
    return os.path.join(_data_dir(), "risk_bypass.json")


def _audit_path() -> str:
    return os.path.join(_data_dir(), "risk_bypass_audit.jsonl")


def _now() -> float:
    return time.time()


def _load() -> dict[str, Any]:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, ValueError):
        pass
    return {}


def _write_audit(record: dict[str, Any]) -> None:
    try:
        with open(_audit_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def is_bypassing(*, now: Optional[float] = None) -> bool:
    """True only while a non-expired operator bypass is armed.

    An expired record is lazily cleared and audited on read. Fail-safe: any
    unexpected state returns False.
    """
    try:
        ts = _now() if now is None else now
        with _LOCK:
            state = _load()
            if not state.get("armed"):
                return False
            expires = float(state.get("expires_epoch") or 0.0)
            if ts < expires:
                return True
            # Expired — clear and leave an audit trail.
            state["armed"] = False
            _persist(state)
            _write_audit({
                "ts": int(ts * 1000),
                "action": "auto_expire",
                "set_by": state.get("set_by"),
            })
        return False
    except Exception as e:  # pragma: no cover - defensive, fail-safe
        logger.warning("[risk_bypass] read failed, not bypassing: %s", e)
        return False


def _persist(state: dict[str, Any]) -> None:
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def arm(*, set_by: str = "operator", ttl_seconds: Optional[int] = None,
        now: Optional[float] = None) -> dict[str, Any]:
    """Arm the bypass (operator/admin). Returns the new state."""
    ts = _now() if now is None else now
    ttl = TTL_SECONDS if ttl_seconds is None else max(1, int(ttl_seconds))
    state = {"armed": True,
             "set_by": str(set_by),
             "armed_epoch": ts,
             "expires_epoch": ts + ttl}
    with _LOCK:
        _persist(state)
        _write_audit({"ts": int(ts * 1000), "action": "arm",
                      "set_by": str(set_by), "ttl_seconds": ttl})
    return state


def disarm(*, set_by: str = "operator", now: Optional[float] = None) -> None:
    """Turn the bypass off immediately."""
    ts = _now() if now is None else now
    with _LOCK:
        state = _load()
        if state.get("armed"):
            state["armed"] = False
            _persist(state)
            _write_audit({"ts": int(ts * 1000), "action": "disarm",
                          "set_by": str(set_by)})
