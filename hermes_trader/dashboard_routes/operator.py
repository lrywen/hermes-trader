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
from hermes_trader.agents import dsl_exit
from hermes_trader.agents.config_store import read_agent_config
from hermes_trader.dashboard import (
    _TERMINAL_HANDLERS,
    _config_apply,
    _log_operator_action,
    _require_operator,
    _terminal_llm_chat,
)

logger = logging.getLogger("hermes-dashboard")


def register_operator_routes(app: FastAPI) -> None:
    """Mount the token-gated operator action routes."""

    @app.get("/api/dashboard/operator/config")
    async def operator_config(request: Request) -> JSONResponse:
        _require_operator(request)
        return JSONResponse(read_agent_config())

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
        # F8: normalize bare tickers to upper-case but preserve HIP-3 namespaced
        # coins (e.g. "xyz:vntl") exactly as given — upper-casing the namespace
        # breaks close_position_market's market routing. Same rule as terminal.
        coin = body.get("coin") or ""
        if ":" not in coin:
            coin = coin.upper()
        if not coin:
            raise HTTPException(400, "coin required")
        from hermes_trader.agents.executor import close_position_market
        # Run the blocking exchange call off the event loop (same pattern as
        # mode/config writes), then F22: persist the manual-close audit event
        # regardless of ok/noop so failed attempts are traceable too.
        result = await asyncio.to_thread(close_position_market, coin)
        _log_operator_action("close", via="web", coin=coin, result=result)
        logger.info("manual close via dashboard: %s -> %s", coin,
                    {k: result.get(k) for k in ("ok", "noop", "error") if k in result})
        return JSONResponse(result)

    @app.post("/api/dashboard/operator/mode")
    async def operator_mode(request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        # F7: a malformed JSON body must return 422, not a 500.
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "invalid JSON body")
        mode = (body.get("mode") or "").upper()
        if mode not in {"OFF", "LIVE"}:
            raise HTTPException(400, "mode must be OFF or LIVE")
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
