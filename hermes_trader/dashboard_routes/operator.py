"""Token-gated operator console routes (F23).

Moved verbatim out of ``dashboard.register_routes``: operator config read,
DSL tracker inspection (F23: via dsl_exit public interfaces), manual close,
mode switch and the command-center terminal. All routes require the
operator token.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from hermes_trader import session_log
from hermes_trader.agents import dsl_exit, manual_control, manual_execution
from hermes_trader.agents.config_store import read_agent_config
from hermes_trader.dashboard import (
    _TERMINAL_HANDLERS,
    _config_apply,
    _issue_feed_ticket,
    _log_operator_action,
    _require_operator,
    _terminal_llm_chat,
)

logger = logging.getLogger("hermes-dashboard")


def _actor(request: Request) -> str:
    return str(request.headers.get("x-portal-user") or "operator")


def _normalized_coin(value: object) -> str:
    coin = str(value or "")
    return coin if ":" in coin else coin.upper()


def register_operator_routes(app: FastAPI) -> None:
    """Mount the token-gated operator action routes."""

    @app.get("/api/dashboard/operator/config")
    async def operator_config(request: Request) -> JSONResponse:
        _require_operator(request)
        # Off the event loop: read_agent_config takes a flock on the config
        # lock file; a held LOCK_EX elsewhere would freeze the loop here.
        return JSONResponse(await asyncio.to_thread(read_agent_config))

    @app.post("/api/dashboard/operator/feed-ticket")
    async def operator_feed_ticket(request: Request) -> JSONResponse:
        # D-FCFG-3: mint a short-lived read-only SSE ticket. The browser
        # EventSource API cannot set Authorization headers, so the UI exchanges
        # its operator token (in a header here) for a ticket it can pass as
        # ?ticket= on the feed stream. Read gate (no write rate-limit) — a
        # ticket grants nothing but feed read access and expires in ~60s.
        _require_operator(request)
        ticket, expires_in_s = _issue_feed_ticket()
        return JSONResponse({"ticket": ticket, "expires_in_s": expires_in_s})

    @app.get("/api/dashboard/operator/trackers")
    async def operator_trackers(request: Request) -> JSONResponse:
        _require_operator(request)
        # F12: the dashboard is a separate process from the trading loop, so
        # disk state MUST be re-read to show live trackers. dsl_exit throttles
        # force-reloads (LOCK_SH + clear + re-parse) to once per interval so
        # rapid polls don't contend with the loop's LOCK_EX writes; an explicit
        # operator refresh (?refresh=true) bypasses that throttle.
        if request.query_params.get("refresh") == "true":
            dsl_exit.reset_force_load_throttle()
        dsl_exit.load_state(force=True)
        # F23: read via public snapshot accessor instead of poking
        # _active_positions / tracker._last_floor directly.
        return JSONResponse(dsl_exit.active_tracker_snapshots())

    @app.post("/api/dashboard/operator/close")
    async def operator_close(request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        # F7: a malformed JSON body must return 422, not a 500.
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "invalid JSON body")
        coin = _normalized_coin(body.get("coin"))
        if not coin:
            raise HTTPException(400, "coin required")
        # S5: dashboard never crosses the process boundary to place an order.
        # The trading loop consumes this command after its normal safety gates.
        result = await asyncio.to_thread(
            manual_control.enqueue_command, "close", {"coin": coin},
            actor=_actor(request), reason=str(body.get("reason") or ""),
        )
        return JSONResponse({"ok": True, "queued": True, "command": result})

    @app.post("/api/dashboard/operator/mode")
    async def operator_mode(request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        # F7: a malformed JSON body must return 422, not a 500.
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "invalid JSON body")
        mode = (body.get("mode") or "").upper()
        # SHADOW runs the full scan/research/LLM pipeline but places no real
        # orders (shadow book). PAPER is deliberately rejected: the executor
        # only shadows on the literal "SHADOW" and skips only on "OFF", so an
        # unknown value like "PAPER" would be treated as LIVE and trade real
        # funds. The UI labels SHADOW as "模拟盘" but always submits "SHADOW".
        if mode not in {"OFF", "LIVE", "SHADOW"}:
            raise HTTPException(400, "mode must be OFF, LIVE or SHADOW")
        result = await asyncio.to_thread(_config_apply, {"mode": mode})
        old_mode = result["old"].get("mode")
        # F22: a trading-mode switch is a high-value audit event; persist it to
        # the session log so it survives restarts like config_update does.
        session_log.append({
            "event": "mode_switch",
            "ts": int(time.time() * 1000),
            "old": old_mode,
            "new": mode,
            "via": "web",
        })
        logger.info("trading mode switch via dashboard: %s -> %s", old_mode, mode)
        return JSONResponse({"mode": mode})

    @app.get("/api/dashboard/operator/interventions")
    async def operator_interventions(request: Request) -> JSONResponse:
        _require_operator(request)
        return JSONResponse(await asyncio.to_thread(manual_control.snapshot))

    @app.post("/api/dashboard/operator/interventions/mode")
    async def operator_intervention_mode(request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        try:
            body = await request.json()
            result = await asyncio.to_thread(
                manual_control.set_mode, str(body.get("mode") or ""),
                actor=_actor(request), reason=str(body.get("reason") or ""),
            )
            return JSONResponse(result)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(422, "invalid JSON body")

    @app.post("/api/dashboard/operator/interventions/pause")
    async def operator_pause_coin(request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "invalid JSON body")
        coin = _normalized_coin(body.get("coin"))
        if not coin:
            raise HTTPException(400, "coin required")
        result = await asyncio.to_thread(
            manual_control.pause_coin, coin, actor=_actor(request),
            reason=str(body.get("reason") or ""), until=body.get("until"),
        )
        return JSONResponse(result)

    @app.post("/api/dashboard/operator/interventions/resume")
    async def operator_resume_coin(request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "invalid JSON body")
        coin = _normalized_coin(body.get("coin"))
        if not coin:
            raise HTTPException(400, "coin required")
        return JSONResponse(await asyncio.to_thread(
            manual_control.resume_coin, coin, actor=_actor(request),
            reason=str(body.get("reason") or ""),
        ))

    @app.post("/api/dashboard/operator/interventions/enter")
    async def operator_manual_enter(request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "invalid JSON body")
        payload = dict(body)
        payload["coin"] = _normalized_coin(body.get("coin"))
        if not payload["coin"]:
            raise HTTPException(400, "coin required")
        try:
            manual_execution._manual_analysis(payload)
            result = await asyncio.to_thread(
                manual_control.enqueue_command, "manual_enter", payload,
                actor=_actor(request), reason=str(body.get("reason") or ""),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return JSONResponse({"ok": True, "queued": True, "command": result})

    @app.post("/api/dashboard/operator/interventions/{confirmation_id}/decision")
    async def operator_confirmation_decision(confirmation_id: str, request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "invalid JSON body")
        if not isinstance(body.get("approve"), bool):
            raise HTTPException(400, "approve boolean required")
        try:
            result = await asyncio.to_thread(
                manual_control.decide_confirmation, confirmation_id,
                approve=body["approve"], actor=_actor(request),
                reason=str(body.get("reason") or ""),
            )
        except KeyError:
            raise HTTPException(404, "confirmation not found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return JSONResponse(result)

    @app.post("/api/dashboard/operator/interventions/adjust-exit")
    async def operator_adjust_exit(request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "invalid JSON body")
        coin = _normalized_coin(body.get("coin"))
        payload = {
            "coin": coin, "side": body.get("side"),
            "sl_pct": body.get("sl_pct"), "tp_pct": body.get("tp_pct"),
        }
        try:
            manual_execution.validate_adjust_exit(payload)
            result = await asyncio.to_thread(
                manual_control.enqueue_command, "adjust_exit", payload,
                actor=_actor(request), reason=str(body.get("reason") or ""),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return JSONResponse({"ok": True, "queued": True, "command": result})

    @app.post("/api/dashboard/operator/terminal")
    async def operator_terminal(request: Request) -> JSONResponse:
        """Hermes command-center terminal — routes a free-form command line.

        Built-in commands resolve locally (no LLM call): `status`, `pause`,
        `resume`, `close <coin>`, `regime`, `config`, `help`. Anything else
        falls through to Nous Hermes via OpenRouter, primed with a compact
        snapshot of recent agent state so the chat is grounded in the bot's
        actual world. Requires the operator token like every operator route.
        """
        _require_operator(request, write=True)
        # F7: a malformed JSON body degrades to a noop instead of a 500.
        try:
            body = await request.json()
        except Exception:
            body = {}
        cmd = (body.get("command") or "").strip()
        if not cmd:
            return JSONResponse({"response": "", "kind": "noop"})
        parts = cmd.split()
        verb = parts[0].lower()

        # F25: built-in verbs dispatch to module-level handlers via
        # _TERMINAL_HANDLERS. A handler returning None (e.g. `close` / `set`
        # without enough arguments) and any unknown verb fall through to the
        # OpenRouter chat fallback.
        handler = _TERMINAL_HANDLERS.get(verb)
        if handler is not None:
            resp = await handler(parts, cmd)
            if resp is not None:
                return resp

        return await _terminal_llm_chat(cmd)
