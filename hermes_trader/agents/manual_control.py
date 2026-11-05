"""S5 persistent operator controls.

The dashboard writes intent only. The trading-loop process consumes commands and
executes them through the normal executor, so LIVE locks and every hard risk gate
remain in the process that owns order placement.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Optional

from hermes_trader.agents import atomic_io
from hermes_trader.loop_runtime import data_dir as _data_dir

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_VALID_MODES = ("auto", "manual_confirm", "manual_only")
_DEFAULT_CONFIRM_TTL_S = 900.0


def locked_json_update(path: str, default: dict[str, Any], update: Any) -> Any:
    """Cross-process read/modify/write; invalid existing state is never replaced."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            with open(path) as f:
                state = json.load(f)
            if not isinstance(state, dict):
                raise ValueError("manual control state must be an object")
            for key, value in default.items():
                state.setdefault(key, value.copy() if isinstance(value, dict) else value)
        except FileNotFoundError:
            state = json.loads(json.dumps(default))
        state, result = update(state)
        atomic_io.write_json_atomic(path, state, default=str, allow_nan=False)
        return result
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _state_path() -> str:
    return os.path.join(_data_dir(), "manual_control.json")


def _audit_path() -> str:
    return os.path.join(_data_dir(), "manual_control_audit.jsonl")


def _fresh_state() -> dict[str, Any]:
    return {
        "version": 1,
        "mode": "auto",
        "paused_coins": {},
        "manual_exits": {},
        "confirmations": {},
        "commands": {},
    }


def _load(*, strict: bool = False) -> dict[str, Any]:
    try:
        with open(_state_path()) as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("mode") not in _VALID_MODES:
            raise ValueError("invalid manual control state")
        for key in ("paused_coins", "manual_exits", "confirmations", "commands"):
            if not isinstance(data.get(key, {}), dict):
                raise ValueError(f"invalid manual control {key}")
            data.setdefault(key, {})
        return data
    except FileNotFoundError:
        return _fresh_state()
    except (json.JSONDecodeError, OSError, ValueError):
        if strict:
            raise
        logger.exception("[manual_control] failed to load state; read defaults")
        return _fresh_state()


def _audit(action: str, *, actor: str, before: dict[str, Any],
           after: dict[str, Any], reason: str = "") -> None:
    rec = {"ts": int(time.time() * 1000), "action": action,
           "actor": actor, "reason": reason, "before": before, "after": after,
           "from": before.get("mode"), "to": after.get("mode")}
    coin = after.get("coin") or before.get("coin")
    if coin:
        rec["coin"] = coin
    path = _audit_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(json.dumps(rec, ensure_ascii=False, default=str, allow_nan=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def set_mode(mode: str, *, actor: str = "operator", reason: str = "") -> dict[str, Any]:
    if mode not in _VALID_MODES:
        raise ValueError(f"invalid mode: {mode}")

    def update(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        before = {"mode": state.get("mode", "auto")}
        state["mode"] = mode
        _audit("set_mode", actor=actor, before=before,
               after={"mode": mode}, reason=reason)
        return state, {"mode": mode, "previous": before["mode"]}

    with _LOCK:
        return locked_json_update(_state_path(), _fresh_state(), update)


def get_mode() -> str:
    with _LOCK:
        return _load().get("mode", "auto")


def automatic_entry_gate(coin: str) -> dict[str, Any]:
    """Fail-closed production decision; unlike get_mode it never hides bad state."""
    try:
        with _LOCK:
            state = _load(strict=True)
            mode = state["mode"]
            if mode == "manual_only":
                return {"allowed": False, "reason": "manual_only_mode",
                        "confirmation_required": False}
            if coin in state["paused_coins"]:
                return {"allowed": False, "reason": f"coin_paused:{coin}",
                        "confirmation_required": False}
            return {"allowed": True, "reason": "",
                    "confirmation_required": mode == "manual_confirm"}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.error("[manual_control] entry gate fail-closed: %s", exc)
        return {"allowed": False, "reason": "manual_control_unreadable",
                "confirmation_required": False}


def auto_entries_blocked() -> bool:
    return get_mode() == "manual_only"


def confirmation_required() -> bool:
    return get_mode() == "manual_confirm"


def pause_coin(coin: str, *, actor: str = "operator", reason: str = "",
               until: Optional[float] = None) -> dict[str, Any]:
    coin = coin.upper()
    def update(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        before = dict(state["paused_coins"].get(coin) or {})
        rec = {"coin": coin, "paused_at": time.time(), "actor": actor,
               "reason": reason, "until": until}
        state["paused_coins"][coin] = rec
        _audit("pause_coin", actor=actor, before=before, after=rec, reason=reason)
        return state, rec

    with _LOCK:
        return locked_json_update(_state_path(), _fresh_state(), update)


def resume_coin(coin: str, *, actor: str = "operator", reason: str = "") -> dict[str, Any]:
    coin = coin.upper()
    def update(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        before = dict(state["paused_coins"].get(coin) or {})
        was_paused = bool(before)
        state["paused_coins"].pop(coin, None)
        _audit("resume_coin", actor=actor, before=before, after={"coin": coin}, reason=reason)
        return state, {"coin": coin, "paused": False, "was_paused": was_paused}

    with _LOCK:
        return locked_json_update(_state_path(), _fresh_state(), update)


def is_coin_paused(coin: str) -> bool:
    with _LOCK:
        return coin.upper() in _load()["paused_coins"]


def should_skip_auto(coin: str) -> tuple[bool, str]:
    decision = automatic_entry_gate(coin)
    return not decision["allowed"], str(decision["reason"])


def request_confirmation(analysis: dict[str, Any], *, ttl_s: float = _DEFAULT_CONFIRM_TTL_S,
                         actor: str = "auto", now: Optional[float] = None) -> dict[str, Any]:
    """Persist one real, fully risk-gated candidate. Approval reruns all gates."""
    ts = time.time() if now is None else float(now)
    analysis_id = str(analysis.get("id") or "")
    if not analysis_id:
        raise ValueError("analysis.id required")
    ttl = max(1.0, float(ttl_s))

    def update(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        for old in state["confirmations"].values():
            if (old.get("analysis_id") == analysis_id
                    and old.get("status") in ("pending", "approved", "executing")):
                return state, dict(old)
        cid = uuid.uuid4().hex
        rec = {
            "id": cid, "analysis_id": analysis_id,
            "coin": str(analysis.get("coin") or ""),
            "side": str(analysis.get("side") or analysis.get("action") or ""),
            "status": "pending", "created_at": ts, "expires_at": ts + ttl,
            "requested_by": actor, "analysis": dict(analysis),
        }
        state["confirmations"][cid] = rec
        _audit("confirmation_requested", actor=actor, before={},
               after={k: v for k, v in rec.items() if k != "analysis"})
        return state, dict(rec)

    with _LOCK:
        return locked_json_update(_state_path(), _fresh_state(), update)


def _expire_locked(state: dict[str, Any], ts: float) -> bool:
    changed = False
    for rec in state["confirmations"].values():
        if rec.get("status") == "pending" and float(rec.get("expires_at", 0)) <= ts:
            rec["status"] = "expired"
            rec["decided_at"] = ts
            rec["decision_actor"] = "system"
            _audit("confirmation_expired", actor="system", before={"status": "pending"},
                   after={"status": "expired", "id": rec.get("id")})
            changed = True
    return changed


def list_confirmations(*, include_terminal: bool = True,
                       now: Optional[float] = None) -> list[dict[str, Any]]:
    ts = time.time() if now is None else float(now)

    def update(state: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        _expire_locked(state, ts)
        rows = list(state["confirmations"].values())
        if not include_terminal:
            rows = [r for r in rows if r.get("status") in ("pending", "approved", "executing")]
        rows.sort(key=lambda r: float(r.get("created_at", 0)), reverse=True)
        return state, [dict(r) for r in rows]

    with _LOCK:
        return locked_json_update(_state_path(), _fresh_state(), update)


def decide_confirmation(confirmation_id: str, *, approve: bool,
                        actor: str = "operator", reason: str = "",
                        now: Optional[float] = None) -> dict[str, Any]:
    ts = time.time() if now is None else float(now)

    def update(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        rec = state["confirmations"].get(confirmation_id)
        if rec is None:
            raise KeyError("confirmation not found")
        _expire_locked(state, ts)
        if rec.get("status") == "expired":
            raise ValueError("confirmation expired")
        if rec.get("status") != "pending":
            raise ValueError(f"confirmation is {rec.get('status')}")
        before = {"status": "pending"}
        rec.update({"status": "approved" if approve else "rejected",
                    "decided_at": ts, "decision_actor": actor,
                    "decision_reason": reason})
        if approve:
            command = _new_command("confirmed_enter", {
                "confirmation_id": confirmation_id,
            }, actor, reason, ts)
            state["commands"][command["id"]] = command
            rec["command_id"] = command["id"]
        _audit("confirmation_approved" if approve else "confirmation_rejected",
               actor=actor, before=before,
               after={"status": rec["status"], "id": confirmation_id}, reason=reason)
        return state, dict(rec)

    with _LOCK:
        return locked_json_update(_state_path(), _fresh_state(), update)


def _new_command(action: str, payload: dict[str, Any], actor: str,
                 reason: str, ts: float) -> dict[str, Any]:
    return {"id": uuid.uuid4().hex, "action": action, "payload": dict(payload),
            "status": "queued", "created_at": ts, "actor": actor, "reason": reason}


def enqueue_command(action: str, payload: dict[str, Any], *,
                    actor: str = "operator", reason: str = "",
                    now: Optional[float] = None) -> dict[str, Any]:
    if action not in ("manual_enter", "adjust_exit", "close"):
        raise ValueError(f"unsupported command: {action}")
    ts = time.time() if now is None else float(now)

    def update(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        rec = _new_command(action, payload, actor, reason, ts)
        state["commands"][rec["id"]] = rec
        _audit("command_queued", actor=actor, before={},
               after={"id": rec["id"], "action": action, "payload": dict(payload)}, reason=reason)
        return state, dict(rec)

    with _LOCK:
        return locked_json_update(_state_path(), _fresh_state(), update)


def claim_commands(*, limit: int = 20, now: Optional[float] = None) -> list[dict[str, Any]]:
    ts = time.time() if now is None else float(now)

    def update(state: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        claimed: list[dict[str, Any]] = []
        queued = sorted((r for r in state["commands"].values() if r.get("status") == "queued"),
                        key=lambda r: float(r.get("created_at", 0)))
        for rec in queued[:max(0, int(limit))]:
            rec["status"] = "executing"
            rec["started_at"] = ts
            claimed.append(dict(rec))
        return state, claimed

    with _LOCK:
        return locked_json_update(_state_path(), _fresh_state(), update)


def complete_command(command_id: str, *, ok: bool, result: dict[str, Any],
                     actor: str = "trading_loop") -> dict[str, Any]:
    def update(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        rec = state["commands"].get(command_id)
        if rec is None:
            raise KeyError("command not found")
        if rec.get("status") != "executing":
            raise ValueError(f"command is {rec.get('status')}")
        rec.update({"status": "completed" if ok else "failed",
                    "completed_at": time.time(), "result": dict(result)})
        _audit("command_completed" if ok else "command_failed", actor=actor,
               before={"status": "executing"},
               after={"id": command_id, "status": rec["status"], "result": dict(result)})
        return state, dict(rec)

    with _LOCK:
        return locked_json_update(_state_path(), _fresh_state(), update)


def update_confirmation(confirmation_id: str, *, status: str,
                        result: dict[str, Any]) -> None:
    if status not in ("executing", "completed", "failed", "expired"):
        raise ValueError("invalid confirmation execution status")

    def update(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
        rec = state["confirmations"].get(confirmation_id)
        if rec is None:
            raise KeyError("confirmation not found")
        current = rec.get("status")
        allowed = (current in ("approved", "executing")
                   and status in ("executing", "completed", "failed"))
        allowed = allowed or (current == "approved" and status == "expired")
        if not allowed:
            raise ValueError(f"confirmation is {current}")
        rec["status"] = status
        rec["execution_result"] = dict(result)
        rec["executed_at"] = time.time()
        _audit("confirmation_expired" if status == "expired" else "confirmation_execution",
               actor="system" if status == "expired" else "trading_loop", before={},
               after={"id": confirmation_id, "status": status, "result": dict(result)})
        return state, None

    with _LOCK:
        locked_json_update(_state_path(), _fresh_state(), update)


def set_manual_exit(coin: str, *, sl_pct: Optional[float] = None,
                    tp_pct: Optional[float] = None, actor: str = "operator",
                    reason: str = "", now: Optional[float] = None) -> dict[str, Any]:
    """Queue an exit adjustment; SL may only tighten on the execution path."""
    if sl_pct is None and tp_pct is None:
        raise ValueError("at least one of sl_pct/tp_pct required")
    if sl_pct is not None and not 0 < sl_pct <= 100:
        raise ValueError("sl_pct must be in (0,100]")
    if tp_pct is not None and tp_pct <= 0:
        raise ValueError("tp_pct must be > 0")
    coin = coin.upper()
    ts = time.time() if now is None else float(now)

    def update(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        before = dict(state["manual_exits"].get(coin) or {})
        rec = {**before, "coin": coin, "actor": actor, "reason": reason, "updated_at": ts}
        if sl_pct is not None:
            rec["sl_pct"] = sl_pct
        if tp_pct is not None:
            rec["tp_pct"] = tp_pct
        command = _new_command("adjust_exit", {
            "coin": coin, "sl_pct": rec.get("sl_pct"), "tp_pct": rec.get("tp_pct"),
        }, actor, reason, ts)
        state["manual_exits"][coin] = rec
        state["commands"][command["id"]] = command
        rec["command_id"] = command["id"]
        _audit("set_manual_exit", actor=actor, before=before, after=rec, reason=reason)
        return state, dict(rec)

    with _LOCK:
        return locked_json_update(_state_path(), _fresh_state(), update)


def get_manual_exit(coin: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        rec = _load()["manual_exits"].get(coin.upper())
        return dict(rec) if rec else None


def record_manual_exit(coin: str, result: dict[str, Any], *, actor: str = "trading_loop") -> None:
    def update(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
        before = dict(state["manual_exits"].get(coin) or {})
        after = {**before, **dict(result), "updated_at": time.time(), "actor": actor}
        state["manual_exits"][coin] = after
        _audit("manual_exit_applied", actor=actor, before=before, after=after)
        return state, None

    with _LOCK:
        locked_json_update(_state_path(), _fresh_state(), update)


def clear_manual_exit(coin: str, *, actor: str = "operator",
                      reason: str = "") -> dict[str, Any]:
    coin = coin.upper()
    def update(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        before = dict(state["manual_exits"].get(coin) or {})
        state["manual_exits"].pop(coin, None)
        _audit("clear_manual_exit", actor=actor, before=before,
               after={"coin": coin}, reason=reason)
        return state, {"coin": coin, "cleared": bool(before)}

    with _LOCK:
        return locked_json_update(_state_path(), _fresh_state(), update)


def list_commands(*, include_terminal: bool = True) -> list[dict[str, Any]]:
    with _LOCK:
        rows = list(_load()["commands"].values())
        if not include_terminal:
            rows = [r for r in rows if r.get("status") in ("queued", "executing")]
        rows.sort(key=lambda r: float(r.get("created_at", 0)), reverse=True)
        return [dict(r) for r in rows]


def snapshot() -> dict[str, Any]:
    with _LOCK:
        state = _load()
        return {
            "mode": state.get("mode", "auto"),
            "auto_entries_blocked": state.get("mode") == "manual_only",
            "confirmation_required": state.get("mode") == "manual_confirm",
            "paused_coins": {k: dict(v) for k, v in state["paused_coins"].items()},
            "manual_exits": {k: dict(v) for k, v in state["manual_exits"].items()},
            "confirmations": list_confirmations(),
            "commands": list_commands(),
        }


def paused_coins_snapshot() -> dict[str, dict[str, Any]]:
    with _LOCK:
        return {k: dict(v) for k, v in _load()["paused_coins"].items()}


def manual_exits_snapshot() -> dict[str, dict[str, Any]]:
    with _LOCK:
        return {k: dict(v) for k, v in _load()["manual_exits"].items()}


def list_paused() -> list[dict[str, Any]]:
    with _LOCK:
        paused = _load()["paused_coins"]
        return [{"coin": c, **dict(v)} for c, v in sorted(paused.items())]


def list_manual_exits() -> list[dict[str, Any]]:
    with _LOCK:
        exits = _load()["manual_exits"]
        return [{"coin": c, **dict(v)} for c, v in sorted(exits.items())]
