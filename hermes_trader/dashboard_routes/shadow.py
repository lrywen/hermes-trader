"""SHADOW paper-ledger routes (virtual account data viewer).

Serves the isolated shadow_book paper account over the dashboard API:

  * GET  /api/dashboard/shadow/account      — bankroll / margin / positions
  * GET  /api/dashboard/shadow/trades       — open + close fills (newest first)
  * GET  /api/dashboard/shadow/equity-curve — equity/wallet curve points
  * GET  /api/dashboard/shadow/stats        — win rate / PnL / hold analytics
  * POST /api/dashboard/shadow/reset        — wipe back to a fresh bankroll (operator write)
  * POST /api/dashboard/shadow/close        — force-close a paper position (operator write)

Reads are anonymous-safe like summary/positions/closed-trades (the paper book
holds no secrets — it is a virtual account); the dashboard is a separate
process from the trading loop, so every read hot-reloads the on-disk state by
mtime inside shadow_book. Writes require the operator token and are audited to
the session log.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from hermes_trader import session_log
from hermes_trader.agents import shadow_book
from hermes_trader.dashboard import _require_operator

logger = logging.getLogger("hermes-dashboard")


def register_shadow_routes(app: FastAPI) -> None:
    """Mount the SHADOW paper-ledger read + operator-write routes."""

    @app.get("/api/dashboard/shadow/account")
    async def shadow_account() -> JSONResponse:
        payload = await asyncio.to_thread(shadow_book.get_account)
        return JSONResponse(payload)

    @app.get("/api/dashboard/shadow/trades")
    async def shadow_trades(limit: int = Query(200, ge=1, le=2000)) -> JSONResponse:
        payload = await asyncio.to_thread(lambda: shadow_book.get_trades(limit=limit))
        return JSONResponse(payload)

    @app.get("/api/dashboard/shadow/equity-curve")
    async def shadow_equity_curve() -> JSONResponse:
        payload = await asyncio.to_thread(shadow_book.get_equity_curve)
        return JSONResponse(payload)

    @app.get("/api/dashboard/shadow/stats")
    async def shadow_stats() -> JSONResponse:
        payload = await asyncio.to_thread(shadow_book.get_stats)
        return JSONResponse(payload)

    @app.post("/api/dashboard/shadow/reset")
    async def shadow_reset(request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any decode failure maps to 422
            raise HTTPException(422, "invalid JSON body")
        starting_balance = None
        if isinstance(body, dict) and body.get("starting_balance") is not None:
            try:
                starting_balance = float(body.get("starting_balance"))
            except (TypeError, ValueError):
                raise HTTPException(400, "starting_balance must be a positive number")
            if starting_balance <= 0:
                raise HTTPException(400, "starting_balance must be positive")

        # Persist the new default bankroll into the live config block so future
        # restarts honor it. Config writes replace top-level blocks wholesale
        # (no deep merge), so read the current shadow_book block and merge only
        # starting_balance, preserving enabled/fee/max_positions siblings.
        if starting_balance is not None:
            try:
                from hermes_trader.agents.config_store import cfg_get
                from hermes_trader.dashboard import _config_apply

                def _persist_bankroll() -> dict:
                    block = dict(cfg_get("shadow_book", {}) or {})
                    block["starting_balance"] = float(starting_balance)
                    return _config_apply({"shadow_book": block}, backup=True)

                await asyncio.to_thread(_persist_bankroll)
            except Exception as _cfg_e:  # noqa: BLE001 — reset still proceeds
                logger.warning("shadow reset: config persist failed (non-fatal): %s", _cfg_e)

        result = await asyncio.to_thread(shadow_book.reset, starting_balance)
        session_log.append({
            "event": "shadow_reset",
            "ts": int(time.time() * 1000),
            "starting_balance": result.get("starting_balance"),
            "closed_wiped": result.get("closed_wiped"),
            "via": "web",
        })
        logger.warning("shadow paper-ledger RESET via dashboard: %s", result)
        return JSONResponse({"ok": True, **result})

    @app.post("/api/dashboard/shadow/close")
    async def shadow_close(request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any decode failure maps to 422
            raise HTTPException(422, "invalid JSON body")
        coin = (body.get("coin") or "").strip()
        if not coin:
            raise HTTPException(400, "coin required")
        side = (body.get("side") or "").strip().lower() or None
        if side not in (None, "long", "short"):
            raise HTTPException(400, "side must be long or short")
        exit_px = body.get("price")
        if exit_px is not None:
            try:
                exit_px = float(exit_px)
            except (TypeError, ValueError):
                raise HTTPException(400, "price must be a number")
            if exit_px <= 0:
                raise HTTPException(400, "price must be positive")

        fill = await asyncio.to_thread(
            shadow_book.close_now, coin, side, exit_px, "manual_close"
        )
        if fill is None:
            raise HTTPException(404, f"no open paper position for {coin}")
        session_log.append({
            "event": "shadow_manual_close",
            "ts": int(time.time() * 1000),
            "coin": coin,
            "side": side,
            "realized_pnl_usd": fill.get("realized_pnl_usd"),
            "via": "web",
        })
        logger.info("shadow manual close via dashboard: %s %s pnl=%s",
                    coin, side, fill.get("realized_pnl_usd"))
        return JSONResponse({"ok": True, "fill": fill})
