"""Execute S5 operator commands inside the trading-loop process."""

from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Any, Optional

from hermes_trader.agents import dsl_exit, manual_control, shadow_book

logger = logging.getLogger(__name__)


def _manual_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    coin = str(payload.get("coin") or "").strip().upper()
    side = str(payload.get("side") or "").strip().lower()
    if not coin:
        raise ValueError("manual coin is required")
    if side not in ("long", "short"):
        raise ValueError("manual side must be long or short")
    try:
        entry_px = float(payload.get("entry_px"))
        notional = float(payload.get("notional_usd"))
        confidence = float(payload.get("confidence", 0.0) or 0.0)
        composite_score = float(payload.get("composite_score", 0.0) or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("manual numeric fields are invalid") from exc
    if not math.isfinite(entry_px) or entry_px <= 0:
        raise ValueError("manual entry_px must be positive and finite")
    if not math.isfinite(notional) or notional <= 0:
        raise ValueError("manual notional_usd must be positive and finite")
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("manual confidence must be finite and in [0,1]")
    if not math.isfinite(composite_score):
        raise ValueError("manual composite_score must be finite")
    bracket = {}
    for key in ("stop_loss", "take_profit"):
        value = payload.get(key)
        if value is None:
            bracket[key] = None
            continue
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"manual {key} must be numeric") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"manual {key} must be positive and finite")
        bracket[key] = value
    return {
        "id": uuid.uuid4().hex,
        "coin": coin,
        "side": side,
        "action": side,
        "verdict": side.upper(),
        "confidence": confidence,
        "composite_score": composite_score,
        "entry_px": entry_px,
        "stop_loss": bracket["stop_loss"],
        "take_profit": bracket["take_profit"],
        "reasoning": str(payload.get("reasoning") or "manual operator request"),
        "signal_kind": "manual",
        "_manual_approved": True,
        "_manual_notional_usd": notional,
    }


def _replace_tp(coin: str, side: str, size: float, tp_px: float,
                old_oid: Optional[str]) -> dict[str, Any]:
    from hermes_trader.execution.orders import cancel_orders, place_hl_trigger_order

    if old_oid:
        cancel_result = cancel_orders(int(old_oid), coin=coin)
        if not cancel_result.get("ok"):
            return {"ok": False, "error": f"old TP cancel failed: {cancel_result}"}
    result = place_hl_trigger_order(
        coin=coin, is_long_position=(side == "long"), size=size,
        trigger_px=tp_px, kind="tp",
    )
    return {"ok": bool(result.get("ok")), "result": result}


def _adjust_live(coin: str, *, side: Optional[str], sl_pct: Optional[float],
                 tp_pct: Optional[float]) -> dict[str, Any]:
    from hermes_trader.agents import executor

    dsl_exit.load_state(force=True)
    candidates = [t for t in dsl_exit.active_tracker_snapshots()
                  if t["coin"] == coin and (not side or t["side"] == side)]
    if len(candidates) != 1:
        return {"ok": False, "error": "exactly one live tracker required"}
    tracker = candidates[0]
    side = str(tracker["side"])
    entry = float(tracker["entry_px"])
    size = executor._live_abs_szi(coin)
    if size is None or size <= 0:
        return {"ok": False, "error": "live position size unavailable"}
    result: dict[str, Any] = {"ok": True, "coin": coin, "side": side}

    if sl_pct is not None:
        sl_px = entry * (1.0 - float(sl_pct) / 100.0) if side == "long" \
            else entry * (1.0 + float(sl_pct) / 100.0)
        old_sl = tracker.get("sl_px")
        if old_sl is not None and ((side == "long" and sl_px < float(old_sl))
                                   or (side == "short" and sl_px > float(old_sl))):
            return {"ok": False, "error": "manual SL can only tighten"}
        if tracker.get("sl_oid"):
            moved = executor.modify_sl_trigger(
                is_long_position=(side == "long"), size=size,
                new_trigger_px=sl_px, coin=coin, oid=int(tracker["sl_oid"]),
            )
            if not moved.get("ok"):
                return {"ok": False, "error": "exchange SL modification failed",
                        "result": moved}
            oid = str(moved.get("order_id") or "")
            if not oid:
                return {"ok": False, "error": "exchange SL oid missing", "result": moved}
            dsl_exit.set_bracket(coin, side, sl_oid=oid, sl_px=sl_px, sl_size=size)
        else:
            from hermes_trader.execution.orders import place_hl_trigger_order
            placed = place_hl_trigger_order(
                coin=coin, is_long_position=(side == "long"), size=size,
                trigger_px=sl_px, kind="sl",
            )
            if not placed.get("ok"):
                return {"ok": False, "error": "exchange SL placement failed", "result": placed}
            oid = str(placed.get("order_id") or "")
            if not oid:
                return {"ok": False, "error": "exchange SL oid missing", "result": placed}
            dsl_exit.set_bracket(coin, side, sl_oid=oid, sl_px=sl_px, sl_size=size)
        result.update({"sl_px": sl_px, "sl_pct": sl_pct, "sl_oid": oid})

    if tp_pct is not None:
        tp_px = entry * (1.0 + float(tp_pct) / 100.0) if side == "long" \
            else entry * (1.0 - float(tp_pct) / 100.0)
        tp_result = _replace_tp(coin, side, size, tp_px, tracker.get("tp_oid"))
        if not tp_result.get("ok"):
            return {"ok": False, "error": "exchange TP replacement failed",
                    "partial": result, "tp": tp_result}
        oid = str(tp_result["result"].get("order_id") or "")
        if not oid:
            return {"ok": False, "error": "exchange TP oid missing", "partial": result}
        dsl_exit.set_bracket(coin, side, tp_oid=oid, tp_px=tp_px)
        result.update({"tp_px": tp_px, "tp_pct": tp_pct, "tp_oid": oid})
    return result


def validate_adjust_exit(payload: dict[str, Any]) -> dict[str, Any]:
    coin = str(payload.get("coin") or "").strip().upper()
    raw_side = payload.get("side")
    side = str(raw_side).strip().lower() if raw_side not in (None, "") else None
    sl_raw = payload.get("sl_pct")
    tp_raw = payload.get("tp_pct")
    if not coin or (sl_raw is None and tp_raw is None):
        raise ValueError("coin and SL/TP required")
    if side not in (None, "long", "short"):
        raise ValueError("side must be long or short")
    pcts: dict[str, Optional[float]] = {"sl_pct": None, "tp_pct": None}
    for key, raw in (("sl_pct", sl_raw), ("tp_pct", tp_raw)):
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be numeric") from exc
        if not math.isfinite(value) or not 0 < value <= 100:
            raise ValueError(f"{key} must be in (0,100]")
        pcts[key] = value
    return {"coin": coin, "side": side, **pcts}


def adjust_exit(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    try:
        request = validate_adjust_exit(payload)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    coin = request["coin"]
    side = request["side"]
    sl_pct = request["sl_pct"]
    tp_pct = request["tp_pct"]
    mode = str(config.get("mode", "OFF")).upper()
    if mode == "SHADOW":
        result = shadow_book.adjust_bracket(
            coin, side=side, sl_pct=sl_pct, tp_pct=tp_pct)
    elif mode == "LIVE":
        result = _adjust_live(coin, side=side, sl_pct=sl_pct, tp_pct=tp_pct)
    else:
        return {"ok": False, "error": "mode_off"}
    if result.get("ok"):
        manual_control.record_manual_exit(coin, result)
    return result


def _entry_succeeded(result: dict[str, Any]) -> bool:
    if result.get("executed"):
        return True
    shadow = result.get("shadow")
    return (
        result.get("mode") == "SHADOW"
        and result.get("reason") == "shadow_mode_would_execute"
        and isinstance(shadow, dict)
        and bool(shadow.get("opened"))
    )


def _execute(command: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    from hermes_trader.agents import executor

    action = command["action"]
    payload = command.get("payload") or {}
    if action == "confirmed_enter":
        cid = str(payload["confirmation_id"])
        confirmation = next((r for r in manual_control.list_confirmations()
                             if r.get("id") == cid), None)
        if confirmation is None or confirmation.get("status") != "approved":
            return {"executed": False, "reason": "confirmation_not_approved"}
        if float(confirmation.get("expires_at") or 0.0) <= time.time():
            result = {"executed": False, "reason": "confirmation_expired"}
            manual_control.update_confirmation(cid, status="expired", result=result)
            return result
        manual_control.update_confirmation(cid, status="executing", result={})
        analysis = dict(confirmation.get("analysis") or {})
        analysis["_manual_approved"] = True
        analysis.pop("pending_confirmation", None)
        result = executor.maybe_execute(analysis)
        if not isinstance(result, dict):
            result = {"executed": False, "reason": str(result)}
        manual_control.update_confirmation(
            cid, status="completed" if _entry_succeeded(result) else "failed", result=result)
        return result
    if action == "manual_enter":
        return executor.maybe_execute(_manual_analysis(payload))
    if action == "adjust_exit":
        return adjust_exit(config, payload)
    if action == "close":
        coin = str(payload["coin"])
        if str(config.get("mode", "OFF")).upper() == "SHADOW":
            fill = shadow_book.close_now(coin, side=payload.get("side"), reason="manual_close")
            return {"ok": fill is not None, "fill": fill}
        if str(config.get("mode", "OFF")).upper() != "LIVE":
            return {"ok": False, "error": "mode_off"}
        return executor.close_position_market(coin)
    return {"ok": False, "error": f"unsupported action {action}"}


def process_pending_commands(config: dict[str, Any], *, limit: int = 20) -> list[dict[str, Any]]:
    """Consume queued commands once; every outcome is persisted for the UI."""
    outcomes: list[dict[str, Any]] = []
    for command in manual_control.claim_commands(limit=limit):
        try:
            result = _execute(command, config)
            if not isinstance(result, dict):
                result = {"executed": False, "reason": str(result)}
            ok = _entry_succeeded(result) if command["action"] in (
                "manual_enter", "confirmed_enter") else bool(result.get("ok"))
            stored = manual_control.complete_command(command["id"], ok=ok, result=result)
            outcomes.append(stored)
        except Exception as exc:
            logger.exception("[manual_control] command %s failed", command.get("id"))
            result = {"ok": False, "executed": False, "error": str(exc)}
            try:
                if command.get("action") == "confirmed_enter":
                    cid = str((command.get("payload") or {}).get("confirmation_id") or "")
                    confirmation = next((r for r in manual_control.list_confirmations()
                                         if r.get("id") == cid), None)
                    if confirmation and confirmation.get("status") == "executing":
                        manual_control.update_confirmation(cid, status="failed", result=result)
                stored = manual_control.complete_command(command["id"], ok=False, result=result)
                outcomes.append(stored)
            except Exception:
                logger.exception("[manual_control] could not persist command failure")
    return outcomes
