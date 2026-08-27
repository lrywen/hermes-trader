"""Public read-only routes, SSE feed and the Vue SPA static hosting (F23).

Moved verbatim out of ``dashboard.register_routes``. Route bodies are
unchanged; shared payload helpers / middleware headers are imported from
``hermes_trader.dashboard``.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from hermes_trader import session_log
from hermes_trader.agents.config_store import read_agent_config
from hermes_trader.dashboard import (
    _SECURITY_HEADERS,
    _closed_trades_payload,
    _equity_curve_payload,
    _positions_payload,
    _summary_payload,
    _tail_log_sse,
    _ttl_cached,
)

logger = logging.getLogger("hermes-dashboard")

# Vue SPA production build mount point (built with `--base=/web/`).
_WEB_DIST = "/app/web-dist"
# no-store on the SPA shell so a server restart isn't masked by a cached
# index.html that pre-dates the new JS/assets. The JSON endpoints below
# are fine to cache for their poll interval.
_NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

# F26: poll-interval cache TTLs for the JSON endpoints (env-overridable;
# defaults match the prior hard-coded values).
_SUMMARY_TTL_S = float(os.environ.get("HERMES_SUMMARY_TTL_S", "2.0"))
_EQUITY_CURVE_TTL_S = float(os.environ.get("HERMES_EQUITY_CURVE_TTL_S", "30.0"))
_CLOSED_TRADES_TTL_S = float(os.environ.get("HERMES_CLOSED_TRADES_TTL_S", "10.0"))


def register_public_routes(app: FastAPI) -> None:
    """Mount middleware, SPA hosting and public read-only endpoints."""

    # ── Deprecation signal for ?token= query transport (F3) ─────────────
    # Tokens in URLs leak into logs/proxies/browser history. The panels now
    # send the X-Operator-Token header; any API request still carrying
    # ?token= gets a Deprecation header + a one-shot warning per path so the
    # legacy transport can be removed once clients stop using it.
    _deprecated_token_paths: set = set()

    @app.middleware("http")
    async def _deprecate_query_token(request: Request, call_next):
        response = await call_next(request)
        # F9: security headers on every response. CSP on JSON endpoints is
        # harmless; X-Frame-Options/nosniff/Referrer-Policy are global.
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/api/") and request.query_params.get("token"):
            response.headers["Deprecation"] = 'true'
            response.headers["Warning"] = (
                '299 hermes-trader "token query parameter is deprecated; '
                'use the X-Operator-Token header"'
            )
            if request.url.path not in _deprecated_token_paths:
                _deprecated_token_paths.add(request.url.path)
                logger.warning(
                    "[operator] deprecated ?token= query transport used on %s "
                    "(from %s) — migrate client to X-Operator-Token header",
                    request.url.path,
                    request.client.host if request.client else "unknown",
                )
        return response

    # ── Vue SPA (hermes-web) static hosting ─────────────────────────────
    # Production build mounted at /web/. Built with `--base=/web/`.
    if os.path.isdir(_WEB_DIST):

        class _SPAStaticFiles(StaticFiles):
            """StaticFiles that falls back to index.html for unknown paths.

            The Vue router uses history mode, so deep links like /web/positions
            or /web/closed-trades don't correspond to files on disk. The default
            StaticFiles returns 404; this subclass serves the SPA shell instead
            so client-side routing can take over.
            """

            async def get_response(self, path: str, scope: dict) -> Response:
                try:
                    response = await super().get_response(path, scope)
                except StarletteHTTPException as exc:
                    if exc.status_code != 404:
                        raise
                    response = None
                if response is None or response.status_code == 404:
                    # Don't intercept real asset requests (missing CSS/JS
                    # should be a real 404, not the SPA shell).
                    if not path.startswith("assets/"):
                        index = os.path.join(self.directory, "index.html")
                        if os.path.isfile(index):
                            return FileResponse(index,
                                                headers=_NO_CACHE_HEADERS)
                if response is not None:
                    return response
                # Re-raise 404 for genuinely missing assets.
                raise StarletteHTTPException(status_code=404)

        app.mount("/web", _SPAStaticFiles(directory=_WEB_DIST, html=True),
                  name="hermes-web")
        logger.info("hermes-web SPA mounted at /web/ from %s", _WEB_DIST)

    @app.get("/", include_in_schema=False)
    async def public_dashboard() -> RedirectResponse:
        # Root redirects to the Vue SPA (Command Center). All UI surfaces
        # (dashboard / operator console / config viewer) are client-side
        # routes under /web/; deep links like /operator are served the SPA
        # shell by the history-mode catch-all below.
        return RedirectResponse(url="/web/", status_code=302)

    @app.get("/api/dashboard/config")
    async def dashboard_config() -> JSONResponse:
        """Read-only JSON dump of `.agent-config.json` for the /config page.
        Hot-reloads alongside the trading loop (no caching)."""
        cfg = await asyncio.to_thread(read_agent_config)
        return JSONResponse(cfg)

    @app.get("/api/dashboard/summary")
    async def dashboard_summary() -> JSONResponse:
        # Payload reads + parses the full session log on every cache miss
        # (~150ms). Run it in a worker thread so a long read doesn't block
        # the asyncio event loop and starve other routes / SSE clients.
        payload = await asyncio.to_thread(_ttl_cached, "summary", _SUMMARY_TTL_S, _summary_payload)
        return JSONResponse(payload)

    @app.get("/api/dashboard/positions")
    async def dashboard_positions() -> JSONResponse:
        # Cache miss falls back to fetch_account_state (9 serial HL POSTs,
        # ~1.3s on testnet). Run in a worker thread so the event loop stays
        # responsive to / and SSE while the live fetch is in flight.
        payload = await asyncio.to_thread(_positions_payload)
        return JSONResponse(payload)

    @app.get("/api/dashboard/equity-curve")
    async def dashboard_equity_curve(range_s: int = Query(86400, ge=60, le=2_592_000)) -> JSONResponse:
        payload = await asyncio.to_thread(
            _ttl_cached, f"equity-curve:{range_s}", _EQUITY_CURVE_TTL_S,
            lambda: _equity_curve_payload(range_s),
        )
        return JSONResponse(payload)

    @app.get("/api/dashboard/closed-trades")
    async def dashboard_closed_trades(limit: int = Query(20, ge=1, le=200)) -> JSONResponse:
        payload = await asyncio.to_thread(
            _ttl_cached, f"closed-trades:{limit}", _CLOSED_TRADES_TTL_S,
            lambda: _closed_trades_payload(limit),
        )
        return JSONResponse(payload)

    @app.get("/api/feed/stream")
    async def feed_stream() -> StreamingResponse:
        return StreamingResponse(
            _tail_log_sse(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx buffering
                "Connection": "keep-alive",
            },
        )

    @app.get("/api/feed/history")
    async def feed_history(limit: int = 500) -> JSONResponse:
        """Return recent session-log events (oldest first), capped at 5000.

        The portal channel page calls this on mount so reloads/visits restore
        the full recent history instead of only the 500-line SSE replay buffer.
        """
        n = max(1, min(limit, 5000))
        events = await asyncio.to_thread(session_log.tail, n)
        return JSONResponse({"events": events})

    # ── SPA catch-all: serve index.html for any non-API GET route ───────
    # Vue Router uses history mode, so deep links like /positions must
    # return the SPA shell instead of FastAPI's default {"detail":"Not Found"}.
    # API routes (/api/, /metrics, /postmortems) are already registered above
    # and take precedence; this catches everything else.
    _spa_index = os.path.join(_WEB_DIST, "index.html") if os.path.isdir(_WEB_DIST) else ""

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_catch_all(full_path: str) -> Response:
        # Never intercept API or known backend routes.
        # F18: also exclude sse/ — a typo'd SSE path must 404 as JSON/empty
        # instead of silently returning the SPA index HTML.
        if (full_path.startswith("api/") or full_path.startswith("metrics")
                or full_path.startswith("postmortems") or full_path.startswith("web/")
                or full_path.startswith("sse/")):
            raise HTTPException(status_code=404)
        if _spa_index and os.path.isfile(_spa_index):
            return FileResponse(_spa_index, headers=_NO_CACHE_HEADERS)
        # SPA not built: fall back to redirect to root.
        return RedirectResponse(url="/", status_code=302)
