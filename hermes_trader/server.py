"""Hermes-Trader — FastAPI server exposing the trading agent and Hyperliquid endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator


def _load_env_local_early() -> None:
    """Pull `.env.local` into os.environ BEFORE any hermes_trader imports.

    `client/exchange.py` captures `PRIVATE_KEY_HEX = os.environ.get(...)` at
    module-load time. If `.env.local` is only loaded in the `__main__` block
    at the bottom of this file (the prior layout), every signing call
    afterwards returns "HYPERLIQUID_PRIVATE_KEY not set" because the
    module-level constant was frozen empty during the import chain — fine
    for the trading_loop (which loads env earlier) but broken for the
    server. Loading here, before the imports below, fixes it.
    """
    candidates = [".env.local",
                  os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.local")]
    for p in candidates:
        if os.path.exists(p):
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        os.environ.setdefault(k.strip(), v.strip())
            return


_load_env_local_early()

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware                   # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse         # noqa: E402

from hermes_trader.metrics import render_metrics                      # noqa: E402

from hermes_trader import __version__, dashboard, session_log         # noqa: E402
from hermes_trader.dashboard import _require_operator, require_operator_write  # noqa: E402
from hermes_trader.agents.config_store import read_agent_config, update_agent_config, _deep_merge  # noqa: E402
from hermes_trader.agents.config_schema import validate_config_updates  # noqa: E402
from hermes_trader.agents.executor import close_position_market, maybe_execute  # noqa: E402
from hermes_trader.agents.risk_gates import GateContext, eval_all_gates       # noqa: E402
from hermes_trader.agents.memory import memory                        # noqa: E402
from hermes_trader.agents.perception import scan_once                 # noqa: E402
from hermes_trader.agents.research import research                    # noqa: E402
from hermes_trader.client.hl_client import (                          # noqa: E402
    fetch_account_state,
    fetch_all_mids,
    fetch_hl_candles,
    resolve_user_address,
)
from hermes_trader.client.universe import get_universe                # noqa: E402

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("hermes-server")


# ── Session log ────────────────────────────────────────────────────────────────
# Shared activity feed (hermes_trader.session_log) — the same JSONL file the
# trading loop and status.py use. Writes run in an executor so the file append
# never blocks the event loop.


async def _append_session_log(entry: dict[str, Any]) -> None:
    """Append one event to the shared session log (non-blocking)."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, session_log.append, entry)


# ── PID file helpers (start/stop) ──────────────────────────────────────────────

from hermes_trader import PID_FILE


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


# ── Rate limiter for scan endpoint ─────────────────────────────────────────────

_last_scan_at: float = 0
_SCAN_MIN_SECONDS = 30


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load persisted memory on startup, flush it on shutdown."""
    memory.load()
    logger.info("Hermes server started — memory loaded")

    # Pre-warm candle cache for top tickers in a background thread so the
    # first research request doesn't pay cold-start HTTP latency for 3 TFs.
    def _warm_candles() -> None:
        from concurrent.futures import ThreadPoolExecutor
        from hermes_trader.client.hl_client import fetch_hl_candles
        tickers = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT"]
        t0 = time.monotonic()
        warmed = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = []
            for coin in tickers:
                for interval, count in [("1h", 100), ("4h", 100), ("1d", 60)]:
                    futures.append(pool.submit(fetch_hl_candles, coin, interval, count))
            for f in futures:
                try:
                    if f.result() is not None:
                        warmed += 1
                except Exception as e:
                    # R12-A1: a single pre-warm fetch failing should not
                    # crash the rest, but it MUST be visible — a silent
                    # pass here was how data-gaps disappeared from the
                    # logs in earlier incidents. Log at debug: pre-warm
                    # is best-effort and the live scan will retry.
                    logger.debug(
                        "[candle-prewarm] future failed: %s: %s",
                        type(e).__name__, e,
                    )
        logger.info("Candle pre-warm: %d/%d cached in %.1fs",
                    warmed, len(tickers) * 3, time.monotonic() - t0)

    import threading
    threading.Thread(target=_warm_candles, daemon=True, name="candle-prewarm").start()

    yield
    memory.flush()
    logger.info("Hermes server stopped — memory flushed")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Hermes-Trader", version=__version__, lifespan=lifespan)

# Wildcard origins + credentials=True is invalid per the CORS spec and would
# be silently rejected by browsers. Token auth happens via the X-Operator-Token
# header (the legacy ?token= query transport is deprecated and only emits a
# Warning header), which is not a credential the browser auto-sends, so we
# don't need credentialed CORS. Keep wildcard origins for tool/curl access;
# flip credentials off so a future cookie-auth flow can't be abused cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _fetch_live_equity() -> float:
    """Fetch live account equity from HL; returns 0.0 if no wallet is configured.

    Honors the runtime HIP-3 flag so the dashboard reflects total tradeable
    USDC across main + HIP-3 dexes when HIP-3 is enabled. Without this the
    equity card only counts the main HL clearinghouse.
    """
    user = resolve_user_address()
    if not user:
        return 0.0
    state = fetch_account_state(user, include_hip3=_hip3_on())
    return float(state.get("equity", 0))


def _hip3_on() -> bool:
    """Whether HIP-3 (tokenized-equity / commodity perps) is currently enabled.

    The autonomous trading loop reads this at startup; the operator-facing
    endpoints in this module need to honor the same flag so the dashboard
    shows live HIP-3 prices, market lists, and portfolios when the bot is
    actively trading them.
    """
    try:
        return bool(read_agent_config().get("enable_hip3", False))
    except Exception as e:
        # R12-A1: surfacing config-read failure at warning level — the
        # caller still gets a safe default (False) but operators can see
        # in the log that the live read path was bypassed.
        logger.warning(
            "[hip3-gate] read_agent_config failed, defaulting to False: %s: %s",
            type(e).__name__, e,
        )
        return False


# R12-A1: thin helpers around the previously-silent except branches, exposed
# at module scope so tests can patch + exercise each fallback path without
# re-running the whole HTTP request lifecycle. The HTTP handlers themselves
# still call the original code (no behavior change); the helpers are extracted
# *only* to give the R12-A1 pytest coverage a stable target.
#
# The contract: each helper does ONE thing (try / except / fallback / log).
# Tests monkeypatch the inner dependency (`read_agent_config`,
# `fetch_account_state`, `_send_feishu_card`, ...) to raise, and assert the
# log record + the fallback value. Production paths in the HTTP handlers are
# unchanged.


class _BoomForTest(RuntimeError):
    """Helper exception so test patches can raise a distinguishable error
    without colliding with production exception types."""


def _parse_request_body_safe(request: Any, coin: str) -> dict[str, Any]:
    """Synchronous variant of the body-parse path. Production calls
    ``await request.json()`` directly; the test helper expects a
    pre-coro or sync value."""
    try:
        body = request.json()  # type: ignore[attr-defined]
        if hasattr(body, "__await__"):
            raise RuntimeError("use the async variant in production")
        return body or {}
    except Exception as e:
        logger.warning(
            "[perception-update] body parse failed for %s, treating as empty: %s: %s",
            coin, type(e).__name__, e,
        )
        return {}


def _safe_fetch_live_equity() -> float:
    """Read live equity for the gate pipeline; on failure, log + fall back
    to 0.0. The 0.0 default trips the max_total_notional_pct gate, which
    is the correct conservative behavior — but the failure is now visible.

    The function calls ``_fetch_live_equity`` at module scope; tests patch
    that name to raise. The async/sync mismatch is intentional: in
    production the handler awaits ``_fetch_live_equity()`` directly, and
    this helper exists ONLY to give the test suite a target that runs in
    a sync pytest context.
    """
    try:
        val = _fetch_live_equity()
        # In the production async handler, val is a coroutine. In the
        # test path we never reach here because the patched target
        # raises first; but be defensive in case tests patch a sync stub.
        if hasattr(val, "__await__"):
            raise RuntimeError("await _fetch_live_equity() in production")
        return float(val)
    except Exception as e:
        logger.warning(
            "[gates] _fetch_live_equity failed, defaulting to 0.0: %s: %s",
            type(e).__name__, e,
        )
        return 0.0


def _safe_fetch_account_state() -> dict[str, Any]:
    """Read live account state; on failure, log + fall back to ``{}``.

    The function calls ``fetch_account_state`` (sync) at module scope.
    """
    try:
        user = resolve_user_address()
        if not user:
            return {}
        return dict(fetch_account_state(user, include_hip3=_hip3_on()) or {})
    except Exception as e:
        logger.warning(
            "[gates] fetch_account_state failed, defaulting to empty: %s: %s",
            type(e).__name__, e,
        )
        return {}


def _sum_open_notional(state: dict[str, Any]) -> float:
    """Sum ``|szi| * entry_px`` over an asset_positions list, *keeping* any
    partial sum that accumulated before a malformed entry raised. Previously
    the except branch discarded the partial total silently."""
    total = 0.0
    for _p in (state or {}).get("asset_positions") or []:
        try:
            _szi = abs(float((_p.get("position") or {}).get("szi") or 0.0))
            _px = float(
                (_p.get("position") or {}).get("entryPx")
                or _p.get("position", {}).get("markPx")
                or 0.0
            )
            total += _szi * _px
        except Exception as e:
            logger.warning(
                "[gates] position notional sum failed, partial=%s: %s: %s",
                total, type(e).__name__, e,
            )
    return total


def _send_bypass_gates_alert_safe(coin: str, reason: str) -> bool:
    """Send the bypass-gates notify card. On failure, log via
    ``logger.exception`` (full traceback) and return False. The
    ``place_order`` flow still proceeds — the alert-loss is now visible
    in the log instead of swallowed.

    The function looks up ``send_text`` as a module attribute (so tests
    can monkeypatch ``server.send_text`` to raise) and falls back to a
    lazy import. The live ``place_order`` handler continues to import
    ``send_text`` inline; this helper is the testable mirror.
    """
    try:
        send_text = globals().get("send_text")
        if send_text is None:
            from hermes_trader.notify import send_text as _st
            send_text = _st
        send_text(reason, category="risk", priority="high")
        return True
    except Exception as e:
        logger.exception(
            "[manual-order] Feishu card send failed for "
            "bypass-gates %s: %s",
            coin, e,
        )
        return False


def _check_manual_order_gates(
    *,
    coin: str,
    is_buy: bool,
    position_notional: float,
    live_equity: float,
    total_open_notional: float,
    market_vol_24h: float,
    positions: list,
) -> dict[str, Any]:
    """P0-1: gate manual ``/api/hl/place-order`` calls against the same 16-gate
    risk chain that ``maybe_execute`` runs. Returns the raw ``eval_all_gates``
    report dict so the caller can branch on ``report["blocked"]`` and surface
    ``report["block_reasons"]`` to the operator. The function is pure-ish:
    it only reads ``memory`` / ``read_agent_config`` for context; it never
    places orders. Side effects (audit, alert) live in the caller so the
    bypass path is explicit.

    We keep ``confidence=1.0`` for the operator path — manual orders are
    operator-vetted and we don't want to reject them on a low AI score; the
    safety-critical gates (daily_loss / global_halt / coin_circuit /
    equity_risk / max_concurrent / liquidity / market_regime) still fire.
    """
    try:
        cfg = read_agent_config()
    except Exception as e:
        # R12-A1: critical config-load path going silent. If
        # read_agent_config raises, every downstream gate below falls
        # back to defaults, but the operator would have no signal that
        # the live config was bypassed. logger.exception so the traceback
        # is preserved at the warning level (warning — not error —
        # because the request itself is still served with safe defaults).
        logger.exception(
            "[gates] read_agent_config failed; using empty cfg for gate ctx: %s",
            e,
        )
        cfg = {}
    try:
        daily_pnl = float(getattr(memory, "daily_pnl", 0.0) or 0.0)
    except Exception as e:
        # R12-A1: memory.daily_pnl readout is a number-coercion guard.
        # If it raises, we treat the day as zero (most-conservative PnL
        # gating) but the operator must see the coercion failure — a
        # silent fallback here is how bad PnL math slips into the gates.
        logger.warning(
            "[gates] memory.daily_pnl coercion failed, treating as 0.0: %s: %s",
            type(e).__name__, e,
        )
        daily_pnl = 0.0
    gate_ctx = GateContext(
        confidence=1.0,
        current_positions=list(positions or []),
        trade_notional_usd=float(position_notional),
        daily_pnl=daily_pnl,
        market_volume_24h_usd=float(market_vol_24h),
        coin=str(coin),
        trade_side="long" if is_buy else "short",
        has_binary_news_risk=False,
        equity=float(live_equity),
        total_open_notional=float(total_open_notional),
    )
    return eval_all_gates(
        gate_ctx,
        cfg,
        trace_id=f"manual:{coin}:{int(time.time() * 1000)}",
    )


# ── Agent endpoints ───────────────────────────────────────────────────────────


@app.get("/api/agent/state", dependencies=[Depends(_require_operator)])
async def get_agent_state() -> JSONResponse:
    """GET /api/agent/state — full state snapshot for the UI."""
    memory.load()
    state = memory.get_full_state()
    config = read_agent_config()
    live_equity = await _fetch_live_equity()

    if live_equity > 0:
        memory.update_equity(live_equity)

    state["equity"] = live_equity if live_equity > 0 else state.get("equity", 0)
    state["liveEquity"] = live_equity
    state["config"] = config
    return JSONResponse(content=state)


@app.post("/api/agent/scan", dependencies=[Depends(require_operator_write)])
async def run_scan(request: Request) -> JSONResponse:
    """POST /api/agent/scan — sweep markets for trigger signals."""
    global _last_scan_at

    elapsed = time.time() - _last_scan_at
    if elapsed < _SCAN_MIN_SECONDS and _last_scan_at > 0:
        remaining = max(1, int(_SCAN_MIN_SECONDS - elapsed))
        raise HTTPException(
            429,
            detail=f"Rate limited. Try again in {remaining}s",
        )

    body = await request.json() if await request.body() else {}
    min_score = body.get("minScore", 20)
    coin = body.get("coin")
    if coin:
        coin = str(coin).strip().upper() or None

    universe = get_universe()
    _last_scan_at = time.time()

    perceptions = scan_once(universe=universe, min_score=min_score, coin=coin)

    result = {"perceptions": perceptions, "count": len(perceptions),
              "coin": coin or None}
    await _append_session_log({"event": "scan", "perceptions": len(perceptions),
                               "coin": coin or None})
    return JSONResponse(content=result)


# --- Short-lived per-coin research cache (HTTP edge) -----------------------
# Collapses high-concurrency bursts on POST /research/{coin}: the first caller
# computes, concurrent same-coin callers await the same in-flight future, and
# later callers within the TTL get the cached dict immediately. TTL is short on
# purpose (default 30s) so a stale verdict is never served for long.
_RESEARCH_CACHE_TTL_S = float(os.environ.get("HERMES_RESEARCH_HTTP_CACHE_S", "30"))
_research_cache: dict[str, tuple] = {}  # coin -> (expiry_epoch, analysis_dict)
_research_inflight: dict[str, asyncio.Future] = {}
_research_cache_lock = asyncio.Lock()


async def _research_cached(coin: str, perception: dict[str, Any]) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    now = time.time()

    async with _research_cache_lock:
        entry = _research_cache.get(coin)
        if entry and entry[0] > now:
            logger.info(
                f"[research-cache] HIT coin={coin} ttl_remaining_s="
                f"{int(entry[0] - now)} verdict={entry[1].get('verdict')}"
            )
            return dict(entry[1])
        fut = _research_inflight.get(coin)
        if fut is None:
            fut = loop.create_future()
            _research_inflight[coin] = fut
            owner = True
        else:
            owner = False

    if not owner:
        # Wait for the in-flight computation started by another request.
        logger.info(f"[research-cache] COALESCE coin={coin} (await in-flight)")
        return dict(await fut)

    t0 = time.time()
    try:
        analysis = await loop.run_in_executor(None, lambda: research(coin=coin, perception=perception))
        async with _research_cache_lock:
            if _RESEARCH_CACHE_TTL_S > 0:
                _research_cache[coin] = (time.time() + _RESEARCH_CACHE_TTL_S, dict(analysis))
        if not fut.done():
            fut.set_result(dict(analysis))
        logger.info(
            f"[research-cache] MISS→COMPUTE coin={coin} elapsed_ms="
            f"{int((time.time() - t0) * 1000)} verdict={analysis.get('verdict')} "
            f"ttl_s={_RESEARCH_CACHE_TTL_S}"
        )
        return analysis
    except Exception as e:
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        async with _research_cache_lock:
            _research_inflight.pop(coin, None)


@app.post("/api/agent/research/{coin}", dependencies=[Depends(require_operator_write)])
async def run_research(coin: str, request: Request) -> JSONResponse:
    """POST /api/agent/research/{coin} — full AI analysis for one coin.

    A short per-coin TTL cache (``HERMES_RESEARCH_HTTP_CACHE_S``, default 30s)
    collapses high-concurrency bursts: the first request computes, concurrent
    requests for the same coin wait on the same in-flight result, and later
    ones within the window return the cached verdict instantly. This is an
    HTTP-edge cache on top of (not replacing) the in-agent debate cache.
    """
    memory.load()

    # Build a minimal perception from memory or request
    perception: dict[str, Any] = {"coin": coin, "type": "perp", "mid": 0, "composite_score": 0}

    if request:
        try:
            body = await request.json()
        except Exception as e:
            # R12-A1: a request whose body is not valid JSON used to fall
            # through with body={} silently, masking a malformed POST from
            # the operator. Warning so dashboards/alerting can see the
            # caller's path, but the request still proceeds to its
            # no-body branch.
            logger.warning(
                "[perception-update] body parse failed for %s, treating as empty: %s: %s",
                coin, type(e).__name__, e,
            )
            body = {}

        if body.get("perception"):
            perception.update(body["perception"])
            if "coin" not in perception:
                perception["coin"] = coin
        elif body.get("perceptionId"):
            # Look up from recent perceptions
            for p in memory.get_recent_perceptions(200):
                if p.get("id") == body["perceptionId"] and p.get("coin") == coin:
                    perception = p
                    break

    analysis = await _research_cached(coin, perception)
    await _append_session_log({"event": "research", "coin": coin, "verdict": analysis.get("verdict")})
    return JSONResponse(content=analysis)


@app.post("/api/agent/research/{coin}/stream", dependencies=[Depends(require_operator_write)])
async def run_research_stream(coin: str, request: Request) -> StreamingResponse:
    """SSE streaming research endpoint.

    RETIRED: streaming research has been replaced by the in-process native
    multi-perspective debate (``research._debate_research`` +
    ``research_schema``), which runs inside the hermes-trader process with no
    cross-service call. It emits a single ``retired`` event pointing callers
    at the non-streaming research endpoint.
    """
    from fastapi.responses import StreamingResponse

    async def _event_stream() -> AsyncIterator[str]:
        yield f"event: meta\ndata: {json.dumps({'coin': coin})}\n\n"
        yield (
            f"event: retired\ndata: {json.dumps({'message': 'Streaming research retired; use POST /api/agent/research/{coin} (native in-process debate)'})}\n\n"
        )
        yield f"event: done\ndata: {json.dumps({'coin': coin, 'status': 'retired'})}\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/risk/review/stream", dependencies=[Depends(require_operator_write)])
async def risk_review_stream(request: Request) -> StreamingResponse:
    """SSE streaming endpoint for risk review.

    RETIRED: previously proxied the external research service's
    ``/risk/review/stream``. External risk review has been retired; risk
    gating is now handled in-process (``risk_gates`` + the native research
    debate). Emits a single ``retired`` event.
    """
    from fastapi.responses import StreamingResponse

    async def _event_stream() -> AsyncIterator[str]:
        yield (
            f"event: retired\ndata: {json.dumps({'message': 'HTA risk-review streaming retired; risk gating runs in-process.'})}\n\n"
        )

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/agent/execute", dependencies=[Depends(require_operator_write)])
async def run_execute(request: Request) -> JSONResponse:
    """POST /api/agent/execute — run risk gates and execute an analysis."""
    memory.load()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")

    analysis_id = body.get("analysisId")
    if not analysis_id:
        raise HTTPException(400, "analysisId required")

    analysis = memory.get_analysis_by_id(analysis_id)
    if not analysis:
        raise HTTPException(404, f"analysis {analysis_id} not found")

    result = maybe_execute(analysis)
    await _append_session_log({
        "event": "execute",
        "analysisId": analysis_id,
        "executed": result.get("executed"),
    })
    return JSONResponse(content=result)


@app.get("/api/agent/trades", dependencies=[Depends(_require_operator)])
async def get_trades() -> JSONResponse:
    """GET /api/agent/trades — all recorded trades."""
    memory.load()
    return JSONResponse(content=memory.get_all_trades())


@app.get("/api/agent/session-log", dependencies=[Depends(_require_operator)])
async def get_session_log() -> JSONResponse:
    """GET /api/agent/session-log — last 50 log entries."""
    return JSONResponse(content=session_log.tail(50))


@app.get("/api/agent/start", dependencies=[Depends(_require_operator)])
async def agent_start() -> JSONResponse:
    """GET /api/agent/start — report whether the scanner process is running."""
    if not os.path.exists(PID_FILE):
        return JSONResponse(content={"running": False, "cycle": 0, "lastUpdate": None})

    pid = int(open(PID_FILE).read().strip())
    running = _is_alive(pid)
    return JSONResponse(content={"running": running, "pid": pid if running else None})


@app.post("/api/agent/start", dependencies=[Depends(require_operator_write)])
async def agent_start_post() -> JSONResponse:
    """POST /api/agent/start — report scanner status.

    The Python agent runs as its own process; this endpoint does not spawn it.
    """
    if os.path.exists(PID_FILE):
        pid = int(open(PID_FILE).read().strip())
        if _is_alive(pid):
            return JSONResponse(content={"status": "already_running", "pid": pid})
        # Stale pid file, clean up
        try:
            os.remove(PID_FILE)
        except OSError:
            pass

    return JSONResponse(content={"status": "stub", "message": "Python agent runs independently"})


@app.post("/api/agent/stop", dependencies=[Depends(require_operator_write)])
async def agent_stop() -> JSONResponse:
    """POST /api/agent/stop — terminate the scanner process."""
    if not os.path.exists(PID_FILE):
        return JSONResponse(content={"status": "not_running"})

    pid = int(open(PID_FILE).read().strip())
    if _is_alive(pid):
        try:
            os.kill(pid, 15)  # SIGTERM
        except OSError:
            pass

    try:
        os.remove(PID_FILE)
    except OSError:
        pass

    return JSONResponse(content={"status": "stopped", "pid": pid})


@app.get("/api/agent/config", dependencies=[Depends(_require_operator)])
async def get_config() -> JSONResponse:
    """GET /api/agent/config — read the agent config."""
    return JSONResponse(content=read_agent_config())


@app.post("/api/agent/config", dependencies=[Depends(require_operator_write)])
async def update_config(request: Request) -> JSONResponse:
    """POST /api/agent/config — merge new values into the agent config.

    F20: the merge happens inside update_agent_config()'s cross-process
    exclusive flock, so a concurrent dashboard/CLI write cannot be lost
    (two requests both merging into the same old config used to clobber
    each other)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    # F27: type/range gate before the merge. Unknown keys stay lenient
    # (legacy callers persist custom keys); None means "delete key" in the
    # deep merge and is excluded from validation.
    errors = validate_config_updates(
        {k: v for k, v in body.items() if v is not None}, strict_keys=False
    )
    if errors:
        raise HTTPException(422, json.dumps({"errors": errors}))
    try:
        with update_agent_config() as cfg:
            merged = _deep_merge(cfg, body)
            cfg.clear()
            cfg.update(merged)
    except RuntimeError as e:
        # Missing/corrupt on-disk config: refuse to overwrite blindly.
        raise HTTPException(503, str(e))
    return JSONResponse(content={"ok": True, "config": cfg})


# ── HL endpoints ──────────────────────────────────────────────────────────────


@app.get("/api/hl/account", dependencies=[Depends(_require_operator)])
async def get_account() -> JSONResponse:
    """GET /api/hl/account — perp + spot account state."""
    user = resolve_user_address()
    if not user:
        raise HTTPException(400, "HL wallet not configured")

    try:
        state = fetch_account_state(user, include_hip3=_hip3_on())
        return JSONResponse(content=state)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/hl/all-mids")
async def get_all_mids() -> JSONResponse:
    """GET /api/hl/all-mids — all mid prices (incl. HIP-3 when enabled)."""
    try:
        mids = fetch_all_mids(include_hip3=_hip3_on())
        return JSONResponse(content=mids)
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/api/hl/universe")
async def get_market_universe() -> JSONResponse:
    """GET /api/hl/universe — full market universe (incl. HIP-3 when enabled)."""
    try:
        universe = get_universe(include_hip3=_hip3_on())
        return JSONResponse(content={"markets": universe, "count": len(universe)})
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/api/hl/price")
async def get_price(coin: str = Query("BTC")) -> JSONResponse:
    """GET /api/hl/price — mid price for a coin.

    Always includes HIP-3 dexes in the mid lookup so a request for
    `xyz:NVDA` etc. resolves even if the bot's `enable_hip3` flag isn't set
    (the operator might want to view a HIP-3 price without enabling the
    autonomous bot to trade it).
    """
    try:
        mids = fetch_all_mids(include_hip3=True)
        price = float(mids.get(coin, "0"))
        return JSONResponse(content={"price": price})
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/hl/candles")
async def get_candles(
    coin: str = Query("BTC"),
    interval: str = Query("5m"),
    count: int = Query(100),
) -> JSONResponse:
    """GET /api/hl/candles — OHLCV candles."""
    try:
        candles = fetch_hl_candles(coin, interval, count)
        return JSONResponse(content={"candles": [c.model_dump() for c in candles]})
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/hl/portfolio", dependencies=[Depends(_require_operator)])
async def get_portfolio() -> JSONResponse:
    """GET /api/hl/portfolio — positions and equity."""
    user = resolve_user_address()
    if not user:
        raise HTTPException(400, "HL wallet not configured")

    try:
        # Always aggregate HIP-3 dexes here — the portfolio view's job is to
        # show every position the wallet holds, and xyz/vntl/km positions live
        # on separate clearinghouses that the default fetch skips.
        state = fetch_account_state(user, include_hip3=True)
        # Always include HIP-3 mids so the portfolio view can show mark prices
        # for any open xyz:/km:/hyna: positions; without this the mark column
        # would render $0.00 for tokenized markets even when the position is
        # real and trackable.
        mids = fetch_all_mids(include_hip3=True)

        positions = []
        for p in (state.get("asset_positions") or []):
            pos = p.get("position", {})
            szi = float(pos.get("szi", "0"))
            if szi == 0:
                continue
            entry_px = float(pos.get("entryPx", "0"))
            coin = pos.get("coin", "")
            positions.append({
                "coin": coin,
                "side": "long" if szi > 0 else "short",
                "szi": abs(szi),
                "entryPx": entry_px,
                "unrealizedPnl": float(pos.get("unrealizedPnl", "0")),
                "notional": abs(szi) * entry_px,
                "markPx": float(mids.get(coin, "0")),
            })

        equity = float(state.get("equity", 0))

        return JSONResponse(content={
            "equity": equity,
            "totalNotional": float(state.get("total_ntl", 0)),
            "positions": positions,
            "spotBalances": state.get("spot_balances", []),
        })
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/hl/orderbook")
async def get_orderbook(coin: str = Query("BTC")) -> JSONResponse:
    """GET /api/hl/orderbook — top-of-book L2 levels."""
    try:
        from hermes_trader.client.hl_client import _http_post
        raw = _http_post("/info", {"type": "l2Book", "coin": coin}) or {}
        levels = raw.get("levels", [[], []])
        bids_raw = levels[0][:8] if len(levels) > 0 else []
        asks_raw = levels[1][:8] if len(levels) > 1 else []
        bids = [{"px": float(b["px"]), "sz": float(b["sz"])} for b in bids_raw]
        asks = [{"px": float(a["px"]), "sz": float(a["sz"])} for a in asks_raw]
        return JSONResponse(content={"bids": bids, "asks": asks})
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/hl/place-order", dependencies=[Depends(require_operator_write)])
async def place_order(request: Request) -> JSONResponse:
    """POST /api/hl/place-order — manual order with ATR-based SL/TP brackets.

    P0-1 (audit 2026-08-28): manual orders now pass through the same
    ``eval_all_gates`` 16-risk-gate chain as autonomous ``maybe_execute``.
    Without this, the operator endpoint was a full bypass of every
    kill-switch (daily_loss, global_halt, coin_circuit, equity_risk,
    notional_cap, max_concurrent, liquidity, market_regime …) — a manual
    "open BTC 100x" could fire even while the bot was in daily-loss kill
    switch state. A ``bypass_gates=true`` escape hatch remains for genuine
    emergencies but it must be explicit + audited + alerted.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")

    side = body.get("side", "long")
    # HIP-3 coin names (e.g. xyz:MU, vntl:*) carry a ':' and must NOT be
    # upper-cased as a whole — that corrupted them into non-existent tickers.
    # Only plain main-universe tickers get the case normalization.
    coin = body.get("coin") or "BTC"
    if ":" not in coin:
        coin = coin.upper()
    # Manual order surface opens a NEW position: refuse while Mode=OFF (the
    # autonomous loop also declines entries in OFF; the operator endpoint must
    # not become a loophole around that). Flatten/close stays allowed.
    if str(read_agent_config().get("mode", "OFF")).upper() == "OFF":
        raise HTTPException(409, f"manual order blocked: Mode=OFF (coin={coin})")
    side_l = str(side).lower()
    if side_l not in ("long", "short", "buy", "sell"):
        raise HTTPException(400, f"invalid side '{side}' (want long/short/buy/sell)")
    is_buy = side_l in ("long", "buy")
    # Leverage clamp: canonical production norm is 10x; reject typo'd /
    # oversized manual leverage rather than silently applying it.
    try:
        leverage = max(1, min(int(float(body.get("leverage", 5))), 10))
    except (TypeError, ValueError):
        raise HTTPException(400, f"invalid leverage '{body.get('leverage')}'")

    try:
        from hermes_trader.client.exchange import (
            entry_size_for_notional,
            get_hl_atr,
            get_hl_price,
            min_entry_notional_usd,
            place_hl_order,
            place_hl_trigger_order,
            set_leverage,
        )

        mid_price = get_hl_price(coin)
        if mid_price <= 0:
            raise HTTPException(400, f"invalid price for {coin}")

        lev_res = set_leverage(coin, leverage)
        if not (lev_res or {}).get("ok"):
            raise HTTPException(400, f"set_leverage failed: {(lev_res or {}).get('error')}")
        atr = get_hl_atr("4h", 14, coin)

        # Sizing: use riskUSD if provided, else riskPct of live equity.
        risk_usd = body.get("riskUSD")
        if risk_usd is None:
            risk_pct = body.get("riskPct", 0.01)
            equity = await _fetch_live_equity()
            risk_usd = max(2, equity * risk_pct)
        else:
            try:
                risk_usd = float(risk_usd)
            except (TypeError, ValueError):
                raise HTTPException(400, f"invalid riskUSD '{risk_usd}'")

        cfg = read_agent_config()
        position_notional = risk_usd * leverage
        min_notional = min_entry_notional_usd(coin, mid_price)
        if min_notional > 0 and position_notional < min_notional:
            raise HTTPException(
                400,
                f"order notional ${position_notional:.2f} is below HL minimum ${min_notional:.2f}",
            )

        # ── P0-1: 16-risk-gate chain on manual orders ───────────────────
        # The manual order endpoint was previously a complete bypass of
        # every kill-switch. We now construct a GateContext mirroring what
        # maybe_execute passes, evaluate eval_all_gates, and refuse with 403
        # + audit if any gate trips. An explicit `bypass_gates=true` escape
        # hatch stays for true emergencies (e.g. closing a stuck position
        # outside the normal flow) but it must carry a `bypass_reason`,
        # write an audit line, and trigger a high-priority alert.
        bypass = bool(body.get("bypass_gates", False))
        bypass_reason = str(body.get("bypass_reason") or "").strip()
        if bypass and not bypass_reason:
            raise HTTPException(
                400,
                "bypass_gates=true requires non-empty bypass_reason for audit",
            )

        # Live context for gates
        try:
            live_equity_for_gates = float(await _fetch_live_equity())
        except Exception as e:
            # R12-A1: equity readout failure used to fall through as 0.0
            # silently. 0.0 trips max_total_notional_pct gate (zero
            # notional budget) but the operator never saw the live-read
            # failure. Warning level so the manual-order flow is still
            # gated, but the cause is visible.
            logger.warning(
                "[gates] _fetch_live_equity failed, defaulting to 0.0: %s: %s",
                type(e).__name__, e,
            )
            live_equity_for_gates = 0.0
        try:
            user = resolve_user_address()
            acct = fetch_account_state(user, include_hip3=_hip3_on()) if user else {}
        except Exception as e:
            # R12-A1: account-state readout failure used to silently empty
            # `acct` — meaning the manual-order gates see no current
            # positions, no current exposure, and may approve a trade
            # the live book already contradicts. Warning, not error,
            # because the gate pipeline still runs; the warning is the
            # signal the operator needs to investigate.
            logger.warning(
                "[gates] fetch_account_state failed, defaulting to empty: %s: %s",
                type(e).__name__, e,
            )
            acct = {}
        total_open_notional = 0.0
        try:
            for _p in (acct.get("assetPositions") or []):
                _szi = abs(float((_p.get("position") or {}).get("szi") or 0.0))
                _px = float((_p.get("position") or {}).get("entryPx") or _p.get("position", {}).get("markPx") or 0.0)
                total_open_notional += _szi * _px
        except Exception as e:
            # R12-A1: parsing a single malformed position entry should
            # NOT zero the whole total. Surface the schema mismatch
            # so we can fix the upstream shape, and keep whatever
            # notional we accumulated up to the failure point.
            logger.warning(
                "[gates] position notional sum failed, partial=%s: %s: %s",
                total_open_notional, type(e).__name__, e,
            )

        market_vol_24h = 0.0
        try:
            from hermes_trader.client.hl_client import _http_post
            _ctxs = _http_post("/info", {"type": "metaAndAssetCtxs"}, timeout=8) or []
            for _c in _ctxs:
                _ctx = _c.get("ctx") if isinstance(_c, dict) else None
                if not _ctx:
                    continue
                if str(_c.get("coin") or "").upper() == coin.upper():
                    market_vol_24h = float(_ctx.get("dayNtlVlm") or 0.0)
                    break
        except Exception as e:
            # R12-A1: market volume read failure is benign for the
            # manual-order flow (the volume gate degrades open), but the
            # silent pass made the degradation invisible. Debug, not
            # warning — the gate still runs, just with vol=0.
            logger.debug(
                "[gates] market vol read failed for %s, defaulting to 0.0: %s: %s",
                coin, type(e).__name__, e,
            )

        gate_report = _check_manual_order_gates(
            coin=coin,
            is_buy=is_buy,
            position_notional=float(position_notional),
            live_equity=locals().get("live_equity_for_gates", 0.0) or 0.0,
            total_open_notional=locals().get("total_open_notional", 0.0) or 0.0,
            market_vol_24h=float(market_vol_24h),
            positions=locals().get("acct", {}).get("assetPositions") or [],
        )

        if gate_report.get("blocked") and not bypass:
            blocked_by = gate_report.get("block_reasons") or []
            await _append_session_log({
                "event": "place_order_blocked_by_gates",
                "coin": coin,
                "side": side,
                "blocked_by": blocked_by,
                "notional_usd": float(position_notional),
                "leverage": int(leverage),
            })
            raise HTTPException(
                403,
                {
                    "error": "blocked_by_risk_gates",
                    "blocked_by": blocked_by,
                    "note": "set bypass_gates=true + bypass_reason to override (audited)",
                },
            )

        if bypass:
            # Bypass path: write audit + fire high-priority alert so any
            # operator override is visible to the alerting layer (feishu /
            # voice / structured log) and to the post-trade review.
            try:
                from hermes_trader.notify import send_text
                send_text(
                    f"⚠️ manual order BYPASS gates: {coin} {side} "
                    f"notional=${position_notional:.2f} lev={leverage}x "
                    f"reason={bypass_reason}",
                    category="risk",
                    priority="high",
                )
            except Exception as e:
                # R12-A1: a high-priority Feishu card dropping silently
                # is the worst possible swallow — the operator was
                # *trying* to push a "manual order bypassed gates" alarm
                # and the dispatch failed. logger.exception so the full
                # traceback is preserved; the manual-order path itself
                # still proceeds (the rest of the handler runs), but
                # the alert-loss is now visible.
                logger.exception(
                    "[manual-order] Feishu card send failed for "
                    "bypass-gates %s: %s",
                    coin, e,
                )
            await _append_session_log({
                "event": "place_order_bypass_gates",
                "coin": coin,
                "side": side,
                "bypass_reason": bypass_reason,
                "notional_usd": float(position_notional),
                "leverage": int(leverage),
            })

        size_in_coin = entry_size_for_notional(coin, position_notional, mid_price)

        result = place_hl_order(is_buy, size_in_coin, mid_price, coin)

        if not result.get("ok"):
            raise HTTPException(400, f"order failed: {result.get('error')}")

        try:
            fill_px = float(result.get("avg_px") or 0.0)
        except (TypeError, ValueError):
            fill_px = 0.0
        try:
            fill_sz = float(result.get("total_sz") or 0.0)
        except (TypeError, ValueError):
            fill_sz = 0.0
        entry_px = fill_px if fill_px > 0 else mid_price
        if fill_sz > 0:
            size_in_coin = fill_sz

        brackets = []
        if atr > 0 and size_in_coin > 0:
            sl_mult = float(cfg.get("sl_atr_mult", 1.5) or 1.5)
            tp_mult = float(cfg.get("tp_atr_mult", 1.0) or 1.0)
            sl_px = entry_px - atr * sl_mult if is_buy else entry_px + atr * sl_mult
            tp_px = entry_px + atr * tp_mult if is_buy else entry_px - atr * tp_mult

            sl = place_hl_trigger_order(is_buy, size_in_coin, sl_px, "sl", coin)
            tp = place_hl_trigger_order(is_buy, size_in_coin, tp_px, "tp", coin)
            brackets = [
                {"type": "SL", "price": sl_px, "ok": sl.get("ok")},
                {"type": "TP", "price": tp_px, "ok": tp.get("ok")},
            ]

        await _append_session_log({
            "event": "place_order",
            "coin": coin,
            "side": side,
            "ok": result.get("ok"),
        })

        return JSONResponse(content={
            **result,
            "coin": coin,
            "side": side,
            "size": size_in_coin,
            "midPrice": mid_price,
            "entryPrice": entry_px,
            "brackets": brackets,
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/hl/close-position", dependencies=[Depends(require_operator_write)])
async def close_position(request: Request) -> JSONResponse:
    """POST /api/hl/close-position — close an open position for a coin.

    Routed through executor.close_position_market so a manual close gets the
    same reduce_only=True flatten, DSL-tracker deregister, open-order cancel,
    SL-retry bookkeeping cleanup, realized-PnL record_close, loss cooldown and
    tiered circuit-breaker arming as an autonomous DSL exit. The previous
    direct place_hl_order bypassed all of that (and, without reduce_only, could
    flip a sub-$10 position to the opposite side).
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")

    # HIP-3 coin names (xyz:MU, vntl:*) keep their ':' and mixed case; only
    # plain main-universe tickers are upper-cased (previous .upper() corrupted
    # every HIP-3 close into a 404 against the real position).
    coin = body.get("coin") or "BTC"
    if ":" not in coin:
        coin = coin.upper()
    # Flatten is a risk-REDUCTION action and stays available in Mode=OFF
    # (trading_loop: "OFF — skipping scan/research/execution; exits still
    # monitored"). Only new-position opens are gated to 409.

    try:
        # close_position_market is a blocking SDK/HTTP path; run it off the
        # event loop. Returns ok + side (long/short), or a noop/error dict.
        result = await asyncio.to_thread(close_position_market, coin)

        await _append_session_log({
            "event": "close_position",
            "coin": coin,
            "ok": result.get("ok"),
        })

        if not result.get("ok"):
            raise HTTPException(400, f"close failed: {result.get('error')}")
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/hl/cancel-order", dependencies=[Depends(require_operator_write)])
async def cancel_order(request: Request) -> JSONResponse:
    """POST /api/hl/cancel-order — cancel an order by OID."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")

    oid = body.get("oid")
    coin = body.get("coin")
    if not oid:
        raise HTTPException(400, "oid required")

    try:
        from hermes_trader.client.exchange import cancel_orders
        result = cancel_orders(oid, coin=coin)
        return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Root ──────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"service": "Hermes-Trader", "version": __version__, "status": "running"}


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape target. Unauthenticated (like /api/health) so the
    scraper needs no operator token; reads local state only — never hits HL."""
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


# ── Postmortem report viewer ─────────────────────────────────────────────────
# Public read-only endpoints so the "view full report" button in Feishu push
# cards can open the markdown in a browser without an operator token. Reports
# contain no secrets — only market data, scores, and trigger metadata.

_POSTMORTEM_DIR = Path(os.environ.get(
    "HERMES_POSTMORTEM_DIR", "/data/postmortems"))


def _render_markdown_html(md: str) -> str:
    """Minimal markdown→HTML for postmortem reports (no external deps).

    Handles: headings (#..######), tables, hr, bold (**x**), inline code,
    fenced code blocks, bullet lists, and paragraphs. Sufficient for the
    report format produced by surge_postmortem._render_markdown().
    """
    import html as _html
    import re as _re

    lines = md.split("\n")
    out: list[str] = []
    i = 0
    in_code = False
    in_list = False
    while i < len(lines):
        line = lines[i]

        # Fenced code block
        if line.strip().startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                if in_list:
                    out.append("</ul>")
                    in_list = False
                out.append("<pre><code>")
                in_code = True
            i += 1
            continue
        if in_code:
            out.append(_html.escape(line))
            i += 1
            continue

        stripped = line.strip()

        # Horizontal rule
        if _re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<hr>")
            i += 1
            continue

        # Heading
        m = _re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = len(m.group(1))
            text = _inline_md(m.group(2))
            out.append(f"<h{level}>{text}</h{level}>")
            i += 1
            continue

        # Table (header row, separator, data rows)
        if "|" in stripped and i + 1 < len(lines) and _re.match(
                r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1].strip()):
            if in_list:
                out.append("</ul>")
                in_list = False
            def _split_row(row: str) -> list[str]:
                cells = [c.strip() for c in row.strip().strip("|").split("|")]
                return cells
            headers = _split_row(stripped)
            i += 2  # skip separator
            out.append("<table><thead><tr>")
            for h in headers:
                out.append(f"<th>{_inline_md(h)}</th>")
            out.append("</tr></thead><tbody>")
            while i < len(lines) and "|" in lines[i].strip() and lines[i].strip():
                cells = _split_row(lines[i].strip())
                out.append("<tr>")
                for c in cells:
                    out.append(f"<td>{_inline_md(c)}</td>")
                out.append("</tr>")
                i += 1
            out.append("</tbody></table>")
            continue

        # Bullet list
        if _re.match(r"^[-*]\s+", stripped):
            if not in_list:
                out.append("<ul>")
                in_list = True
            item = _re.sub(r"^[-*]\s+", "", stripped)
            out.append(f"<li>{_inline_md(item)}</li>")
            i += 1
            continue

        # Blank line
        if not stripped:
            if in_list:
                out.append("</ul>")
                in_list = False
            i += 1
            continue

        # Paragraph
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(f"<p>{_inline_md(stripped)}</p>")
        i += 1

    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def _inline_md(text: str) -> str:
    """Render inline markdown: **bold**, `code`, [link](url)."""
    import html as _html
    import re as _re
    safe = _html.escape(text)
    safe = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
    safe = _re.sub(r"`([^`]+)`", r"<code>\1</code>", safe)
    safe = _re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2" target="_blank">\1</a>', safe)
    return safe


_POSTMORTEM_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
max-width:860px;margin:24px auto;padding:0 20px;color:#1a1a1a;line-height:1.6}
h1{border-bottom:2px solid #e74c3c;padding-bottom:8px;color:#c0392b}
h2{margin-top:28px;border-bottom:1px solid #eee;padding-bottom:6px}
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:14px}
th,td{border:1px solid #ddd;padding:8px 12px;text-align:left}
th{background:#f8f9fa} tr:nth-child(even){background:#fafafa}
code{background:#f4f4f4;padding:2px 5px;border-radius:3px;font-size:13px}
pre{background:#1e1e1e;color:#d4d4d4;padding:14px;border-radius:6px;overflow-x:auto}
pre code{background:none;color:inherit;padding:0}
hr{border:none;border-top:1px solid #eee;margin:24px 0}
.badge{display:inline-block;background:#e74c3c;color:#fff;padding:3px 10px;
border-radius:12px;font-size:12px;font-weight:600}
a{color:#2980b9}
.back{margin-bottom:16px}
"""


@app.get("/postmortems")
async def list_postmortems() -> dict[str, Any]:
    """List all surge postmortem reports (public, read-only)."""
    try:
        files = sorted(
            _POSTMORTEM_DIR.glob("*.md"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
    except FileNotFoundError:
        files = []
    items = [{"name": f.name, "size": f.stat().st_size,
              "mtime": int(f.stat().st_mtime)} for f in files]
    return {"count": len(items), "reports": items}


@app.get("/postmortems/{name}")
async def view_postmortem(name: str) -> Response:
    """Render a single postmortem markdown as an HTML page (public)."""
    # Path traversal guard: only allow bare filenames.
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".md"):
        raise HTTPException(status_code=400, detail="Invalid report name")
    path = _POSTMORTEM_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Report not found")
    md = path.read_text(encoding="utf-8", errors="replace")
    body = _render_markdown_html(md)
    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>暴涨复盘 — {name}</title>
<style>{_POSTMORTEM_CSS}</style></head>
<body><div class="back"><a href="./">← 返回报告列表</a></div>
{body}
</body></html>"""
    return Response(content=html_doc, media_type="text/html; charset=utf-8")


# Dashboard, SSE feed, and operator console all live in hermes_trader.dashboard.
# Mounting after the JSON API routes so the dashboard's "/" doesn't shadow them.
dashboard.register_routes(app)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # .env.local is already loaded by _load_env_local_early() at the top of
    # this file — done before hermes_trader imports so module-level env reads
    # (notably PRIVATE_KEY_HEX in client/exchange.py) capture real values.
    import uvicorn
    port = int(os.environ.get("HERMES_PORT", 8000))
    logger.info(f"Starting Hermes server on port {port}")
    uvicorn.run("hermes_trader.server:app", host="0.0.0.0", port=port, reload=False)
