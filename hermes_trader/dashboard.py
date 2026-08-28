"""Public + operator web UI for hermes-trader.

Two surfaces, one module:

  GET /                          — redirects to the Vue SPA (/web/)
  GET /web/                      — Vue SPA (dashboard, operator, config)
  GET /api/dashboard/summary     — hero numbers + status
  GET /api/dashboard/positions   — open positions + DSL tracker state
  GET /api/dashboard/equity-curve?range=24h|7d|30d
  GET /api/feed/stream           — Server-Sent Events tailing the session log

All data flows from the same JSONL session log + in-memory DSL registry the
trading loop already maintains, so the UI is read-only by default and there
is no second source of truth to keep in sync.

Operator routes require `HERMES_OPERATOR_TOKEN`; missing/wrong token → 401.
The variable is checked at request time, not import time, so rotating it
doesn't require a restart.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from hermes_trader import event_log, session_log
from hermes_trader.agents import dsl_exit
from hermes_trader.agents.config_schema import (
    coerce_config_value as _coerce_config_value,
    validate_config_updates as _validate_config_updates,
)
from hermes_trader.agents.config_store import (
    cfg_get,
    read_agent_config,
    update_agent_config,
)
from hermes_trader.client.hl_client import fetch_account_state, resolve_user_address
from hermes_trader.positions_snapshot import read_snapshot as read_position_snapshot

logger = logging.getLogger("hermes-dashboard")

_LOG_PATH = Path(session_log.SESSION_LOG_FILE)

# Hyperliquid taker fee — 2.5bps per fill, paid on notional. We close with IOC
# orders so all closes are taker. Round-trip cost on margin: 2 fills × 0.025% × leverage.
HL_TAKER_FEE_PCT = 0.025
HL_ROUND_TRIP_FILLS = 2

# HL per-coin max leverage table, built lazily from one info.meta() call so the
# closed-trades fallback can compute a sane historical leverage estimate
# without spamming the API per row.
# F16: refresh at most once per TTL; a failed fetch must NOT cache an empty
# table for the whole process lifetime (leverage estimates would silently
# degrade to the fallback until restart). Failure keeps the previous good
# table (or None on the very first attempt).
_max_lev_table: Optional[Dict[str, int]] = None
_max_lev_table_loaded_at: float = 0.0
_MAX_LEV_TTL_S = float(os.environ.get("HERMES_MAX_LEV_TTL_S", "3600"))
_max_lev_lock = threading.Lock()

# F20: serializes dashboard-side config read-modify-write. The config writer
# is atomic and flock-guarded per call, but two concurrent requests can both
# read the old config, each tweak their own key, and the later writer clobbers
# the earlier one's change. This lock makes the whole RMW critical section
# atomic within the dashboard process (config writes are rare operator actions,
# so a process-wide lock is sufficient and never contended on the hot path).
_config_rmw_lock = threading.Lock()


def _load_max_lev_table() -> Dict[str, int]:
    global _max_lev_table, _max_lev_table_loaded_at
    now = time.time()
    if _max_lev_table is not None and now - _max_lev_table_loaded_at < _MAX_LEV_TTL_S:
        return _max_lev_table
    with _max_lev_lock:
        now = time.time()
        if _max_lev_table is not None and now - _max_lev_table_loaded_at < _MAX_LEV_TTL_S:
            return _max_lev_table
        try:
            from hermes_trader.client.exchange import _get_info
            meta = _get_info().meta() or {}
            table = {
                u["name"]: int(u.get("maxLeverage", 1) or 1)
                for u in meta.get("universe", []) if "name" in u
            }
            _max_lev_table = table
            _max_lev_table_loaded_at = now
        except Exception:
            logger.warning("[dashboard] max-leverage table fetch failed; "
                           "using %s", "stale table" if _max_lev_table else "fallback defaults")
        return _max_lev_table or {}


# F15: one shared AsyncClient for the OpenRouter chat fallback — per-request
# clients throw away the connection pool (new TCP+TLS handshake every chat).
# http2 + keepalive reuse the connection. A tiny circuit breaker stops hammering
# OpenRouter during an outage: 3 consecutive failures trip it for 5 minutes.
_LLM_CLIENT: Optional["httpx.AsyncClient"] = None
_LLM_CB_FAILURES = 0
_LLM_CB_OPEN_UNTIL = 0.0
_LLM_CB_LOCK = threading.Lock()
_LLM_CB_FAIL_THRESHOLD = int(os.environ.get("HERMES_LLM_CB_FAILURES", "3"))
_LLM_CB_COOLDOWN_S = float(os.environ.get("HERMES_LLM_CB_COOLDOWN_S", "300"))


def _llm_client() -> "httpx.AsyncClient":
    global _LLM_CLIENT
    if _LLM_CLIENT is None or _LLM_CLIENT.is_closed:
        import httpx
        _LLM_CLIENT = httpx.AsyncClient(
            timeout=20.0, http2=True,
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )
    return _LLM_CLIENT


def _llm_circuit_open() -> bool:
    return time.time() < _LLM_CB_OPEN_UNTIL


def _llm_record_success() -> None:
    global _LLM_CB_FAILURES
    with _LLM_CB_LOCK:
        _LLM_CB_FAILURES = 0


def _llm_record_failure() -> None:
    global _LLM_CB_FAILURES, _LLM_CB_OPEN_UNTIL
    with _LLM_CB_LOCK:
        _LLM_CB_FAILURES += 1
        if _LLM_CB_FAILURES >= _LLM_CB_FAIL_THRESHOLD:
            _LLM_CB_OPEN_UNTIL = time.time() + _LLM_CB_COOLDOWN_S
            _LLM_CB_FAILURES = 0
            logger.warning("[dashboard] LLM circuit OPEN for %.0fs after %d consecutive failures",
                           _LLM_CB_COOLDOWN_S, _LLM_CB_FAIL_THRESHOLD)


# F9: defence-in-depth security headers on every response. Applied by
# middleware to the Vue SPA shell and JSON API alike. CSP allows
# 'unsafe-inline' for legacy compatibility; external resources are pinned
# to the CDN origins the UI loads and the single client-side cross-origin
# fetch (FX rates). X-Frame-Options/frame-ancestors block clickjacking;
# nosniff blocks MIME-confusion; referrer hides the operator token query
# param from cross-origin requests.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com "
    "https://cdn.jsdelivr.net https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self' https://open.er-api.com; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _redact(value: Any) -> Any:
    """F17: scrub secrets from anything before it reaches a log line or an
    error response — Authorization headers, Bearer tokens, and *_KEY/*_TOKEN
    fields. Recurses into dicts/lists; non-string scalars pass through."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kl = str(k).lower()
            if "authorization" in kl or "bearer" in kl:
                out[k] = "<redacted>"
            elif (kl in ("key", "token", "api_key", "apikey")
                  or kl.endswith("_key") or kl.endswith("_token")
                  or "api_key" in kl or "apikey" in kl):
                out[k] = "<redacted>"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        # In case a raw key is string-interpolated into a message, mask
        # `Bearer <secret>` and the actual OPENROUTER_API_KEY value if present.
        s = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer <redacted>", value)
        _rk = os.environ.get("OPENROUTER_API_KEY", "")
        if _rk and len(_rk) >= 8 and _rk in s:
            s = s.replace(_rk, "<redacted>")
        return s
    return value


# ── data helpers ─────────────────────────────────────────────────────────────

# Generic TTL cache for read-heavy dashboard endpoints. The dashboard polls
# every few seconds; without this each poll re-reads + re-parses the 800KB+
# session-log JSONL from disk. Keyed by (name, args) so parametrized
# endpoints (equity-curve range, closed-trades limit) cache per-variant.
# F14: per-key singleflight — when N concurrent polls miss the TTL at the same
# instant (asyncio.to_thread pool), only ONE thread runs the loader; the rest
# wait for its result instead of stampeding the disk/HL API.
_TTL_CACHE: Dict[str, tuple] = {}
_TTL_CACHE_LOCK = threading.Lock()
_TTL_INFLIGHT: Dict[str, "threading.Event"] = {}
_TTL_LOAD_WAIT_S = 60.0


def _ttl_cached(key: str, ttl: float, fn: Callable[[], Any]) -> Any:
    now = time.time()
    with _TTL_CACHE_LOCK:
        hit = _TTL_CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        ev = _TTL_INFLIGHT.get(key)
        if ev is None:
            ev = threading.Event()
            _TTL_INFLIGHT[key] = ev
            loader = True
        else:
            loader = False
    if not loader:
        ev.wait(timeout=_TTL_LOAD_WAIT_S)
        with _TTL_CACHE_LOCK:
            hit = _TTL_CACHE.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        # Waiter timed out without a fresh value → load it ourselves.
        with _TTL_CACHE_LOCK:
            ev2 = _TTL_INFLIGHT.get(key)
            if ev2 is None:
                ev2 = threading.Event()
                _TTL_INFLIGHT[key] = ev2
                loader = True
            else:
                loader = False
        if not loader:
            ev2.wait(timeout=_TTL_LOAD_WAIT_S)
            with _TTL_CACHE_LOCK:
                hit = _TTL_CACHE.get(key)
                if hit:
                    return hit[1]
    try:
        val = fn()
        with _TTL_CACHE_LOCK:
            _TTL_CACHE[key] = (time.time(), val)
        return val
    finally:
        with _TTL_CACHE_LOCK:
            done = _TTL_INFLIGHT.pop(key, None)
        if done is not None:
            done.set()


def _read_jsonl_incremental(path: Path, cache: Dict[str, Any], lock: "threading.Lock") -> List[Dict[str, Any]]:
    """F13: incremental JSONL reader. Keeps the parsed lines plus the byte
    offset / inode of the previous read; on the next call only newly appended
    bytes are parsed. Truncation/rotation (inode change or size shrink) resets
    to a full read. A stat() per call is far cheaper than re-reading the whole
    log on every 2s poll."""
    if not path.exists():
        with lock:
            cache.update(lines=[], inode=None, size=-1, offset=0)
        return []
    st = path.stat()
    with lock:
        same = (cache.get("inode") == st.st_ino
                and cache.get("size") == st.st_size)
        if same:
            return cache["lines"]
        rotated = cache.get("inode") != st.st_ino or st.st_size < cache.get("offset", 0)
        offset = 0 if rotated else cache.get("offset", 0)
        lines: List[Dict[str, Any]] = [] if rotated else list(cache.get("lines", []))
    new_lines: List[Dict[str, Any]] = []
    try:
        with path.open() as f:
            f.seek(offset)
            tail = f.read()
            new_offset = f.tell()
        for line in tail.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                new_lines.append(json.loads(line))
            except json.JSONDecodeError:
                # A partially-flushed last line: keep the old offset so it is
                # re-read (and completed) on the next call.
                new_offset = offset + tail.rfind(line)
                break
    except OSError:
        with lock:
            return cache.get("lines", [])
    lines.extend(new_lines)
    with lock:
        cache.update(lines=lines, inode=st.st_ino, size=st.st_size, offset=new_offset)
    return lines


_LOG_CACHE: Dict[str, Any] = {"lines": [], "inode": None, "size": -1, "offset": 0}
_LOG_CACHE_LOCK = threading.Lock()


def _read_log_lines() -> List[Dict[str, Any]]:
    return _read_jsonl_incremental(_LOG_PATH, _LOG_CACHE, _LOG_CACHE_LOCK)


_EVENTS_PATH = Path(event_log.EVENTS_FILE)
_OUTCOME_CACHE: Dict[str, Any] = {"lines": [], "inode": None, "size": -1, "offset": 0}
_OUTCOME_CACHE_LOCK = threading.Lock()


def _read_outcome_lines() -> List[Dict[str, Any]]:
    """Read events.jsonl — the authoritative outcome log that holds reconciled
    `close` events (exchange-triggered / manual-backfill) which may never appear
    in the high-frequency session-log. Each record is nested as
    ``{event, trace_id, timestamp, payload}``. F13: reads incrementally."""
    return _read_jsonl_incremental(_EVENTS_PATH, _OUTCOME_CACHE, _OUTCOME_CACHE_LOCK)


def _iso_to_ms(ts: Any) -> Optional[int]:
    """Parse an ISO-8601 timestamp (as written to events.jsonl) to epoch ms."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        from datetime import datetime, timezone
        s = ts.rstrip("Z")
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception as e:
        # R12-B1: silent fallback masked malformed historical timestamps.
        # The dashboard sorts by ts; a None here just means the row sorts
        # to the tail, which is harmless — but the operator should see
        # that the timestamp column is being misread. Debug, not warning,
        # because the public read path still renders.
        logger.debug("[dashboard] iso-ts parse failed for %r: %s: %s",
                     ts, type(e).__name__, e)
        return None


def _last_event(events: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for e in reversed(events):
        if e.get("event") == name:
            return e
    return None


def _summary_payload() -> Dict[str, Any]:
    """Equity, daily PnL, open count, last-tick — derived from the session log so
    the dashboard works even if the live HL fetch is rate-limited."""
    events = _read_log_lines()
    heartbeat = _last_event(events, "loop_heartbeat") or {}
    last_scan = _last_event(events, "scan")
    # F6: defensive .get('ts', 0) — a trailing parseable dict missing 'ts'
    # must not KeyError this public summary endpoint into a 500.
    last_event_ts = (events[-1].get("ts", 0) if events else 0)

    equity = float(heartbeat.get("equity", 0) or 0)
    daily_pnl = float(heartbeat.get("daily_pnl", 0) or 0)
    # Start-of-day equity = equity - daily_pnl (heartbeat-consistent)
    sod = equity - daily_pnl
    daily_pnl_pct = (daily_pnl / sod * 100) if sod > 0 else 0.0

    now_ms = int(time.time() * 1000)
    last_tick_age_s = max(0, (now_ms - last_event_ts) // 1000) if last_event_ts else None

    # Heuristic status: "scanning" if a heartbeat hit in the last 3min;
    # "stale" if older; "offline" if no heartbeat ever.
    if not heartbeat:
        status = "offline"
    elif last_tick_age_s is None or last_tick_age_s > 180:
        status = "stale"
    else:
        status = "scanning"

    # Per-dex breakdowns so the dashboard can show where USDC sits
    # (e.g. main $96 + xyz $114 + km $20) instead of one opaque total.
    dex_equity = heartbeat.get("dex_equity") or {}
    dex_available = heartbeat.get("dex_available") or {}

    return {
        "equity": round(equity, 2),
        "available": round(float(heartbeat.get("available", 0) or 0), 2),
        "dex_equity": dex_equity,
        "dex_available": dex_available,
        "spot_usdc": round(float(heartbeat.get("spot_usdc", 0) or 0), 2),
        "daily_pnl": round(daily_pnl, 2),
        "daily_pnl_pct": round(daily_pnl_pct, 2),
        "open_positions": int(heartbeat.get("open_positions", 0) or 0),
        "last_tick_age_s": last_tick_age_s,
        "last_scan_triggers": int((last_scan or {}).get("triggers", 0) or 0),
        "status": status,
        "ts": now_ms,
    }


_POSITIONS_CACHE: Dict[str, Any] = {"ts": 0.0, "data": []}
# F26: acceptable staleness for the display positions endpoint (env-overridable).
_POSITIONS_CACHE_TTL_S = float(os.environ.get("HERMES_POSITIONS_CACHE_TTL_S", "5.0"))


def _positions_payload() -> List[Dict[str, Any]]:
    """Join live HL positions with DSL tracker state for the operator/public view.

    Cached for ~5s so repeated dashboard polls don't hammer HL with
    fetch_account_state(include_hip3=True) — each call is ~9 HTTP POSTs
    (1 main + 8 HIP-3 dexes) even with the parallel fan-out. Cache TTL
    is short enough that the position table never feels stuck.
    """
    now = time.time()
    if now - _POSITIONS_CACHE["ts"] < _POSITIONS_CACHE_TTL_S:
        return _POSITIONS_CACHE["data"]
    data = _positions_payload_uncached()
    _POSITIONS_CACHE["ts"] = now
    _POSITIONS_CACHE["data"] = data
    return data


def _positions_payload_uncached() -> List[Dict[str, Any]]:
    dsl_exit.load_state(force=True)

    # Prefer the loop's snapshot: it already fetched account state this cycle,
    # so reading the file avoids a duplicate fetch_account_state (~9 HL POSTs)
    # from this separate process — that duplication was tripping HL's per-IP
    # rate limit. Fall back to a live fetch only when the snapshot is missing
    # or stale (loop not running), so a standalone dashboard still works.
    snap = read_position_snapshot(max_age_s=120.0)
    if snap is not None:
        return _rows_from_state(snap)

    user = resolve_user_address()
    if not user:
        return []
    try:
        # include_hip3=True so xyz:MU / vntl:* positions appear in the
        # dashboard list alongside main-dex positions; HIP-3 dexes are
        # separate clearinghouses that the default fetch ignores.
        state = fetch_account_state(user, include_hip3=True)
    except Exception as e:
        # R12-B1: silent fallback to [] used to make a working dashboard
        # look "flat" the moment the HL account-state fetch started
        # throwing — the operator had no signal that the live read path
        # was bypassed. Warning so alerters see the failure, but the
        # dashboard still renders the rest of the read paths.
        logger.warning(
            "[dashboard] _live_positions fetch_account_state failed: %s: %s",
            type(e).__name__, e,
        )
        return []
    return _rows_from_state(state)


def _parse_raw_position(pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize one HL ``asset_positions[i]["position"]`` record into numeric
    fields. F24: single source of truth shared by the dashboard view rows
    (_rows_from_state) and the terminal command rows (_positions_from_state),
    which used to hand-roll two divergent parsers.

    Returns None for flat (szi==0), coin-less, or unparseable records — a
    single malformed entry must not abort the whole position list.
    """
    coin = pos.get("coin")
    try:
        szi = float(pos.get("szi", "0") or 0)
        entry = float(pos.get("entryPx") or 0)
        unrealized_usd = float(pos.get("unrealizedPnl", 0) or 0)
        margin_used = float(pos.get("marginUsed", 0) or 0)
        position_value = float(pos.get("positionValue", 0) or 0)
        # HL stores leverage as {"value": N, "type": "cross"|"isolated"}; older
        # records (and synthesized stubs) may store it as a bare int.
        leverage_obj = pos.get("leverage")
        if isinstance(leverage_obj, dict):
            leverage = int(leverage_obj.get("value", 1) or 1)
        else:
            leverage = int(leverage_obj or 1)
    except (TypeError, ValueError):
        return None
    if szi == 0 or not coin:
        return None
    return {
        "coin": coin,
        "szi": szi,
        "side": "long" if szi > 0 else "short",
        "entry": entry,
        "mark": (position_value / abs(szi)) if szi else 0.0,
        "unrealized_usd": unrealized_usd,
        "margin_used": margin_used,
        "leverage": leverage,
    }


def _rows_from_state(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Transform a raw HL account state into dashboard position rows, overlaying
    DSL tracker phase/floor from the shared state file. Pure — no network."""
    rows: List[Dict[str, Any]] = []
    for p in state.get("asset_positions") or []:
        parsed = _parse_raw_position(p.get("position", {}))
        if parsed is None:
            continue
        coin = parsed["coin"]
        side = parsed["side"]
        szi = parsed["szi"]
        entry = parsed["entry"]
        mark = parsed["mark"]
        unrealized_usd = parsed["unrealized_usd"]
        margin_used = parsed["margin_used"]
        leverage = parsed["leverage"]

        spot_pct = ((mark - entry) / entry * 100 if side == "long"
                    else (entry - mark) / entry * 100) if entry else 0
        # ROE = unrealizedPnl / marginUsed — this is what HL's "PNL (ROE %)"
        # column displays, and it already accounts for the open-side fee paid.
        roe_pct = (unrealized_usd / margin_used * 100) if margin_used > 0 else spot_pct * leverage

        # F23: read DSL phase/floor via the public accessor instead of
        # poking _active_positions / tracker._last_floor directly.
        dsl_info = dsl_exit.tracker_view(coin, side)

        rows.append({
            "coin": coin,
            "side": side,
            "size": abs(szi),
            "leverage": leverage,
            "entry_px": entry,
            "mark_px": mark,
            "unrealized_pnl_usd": unrealized_usd,
            "unrealized_pct": roe_pct,       # leveraged ROE — matches HL
            "spot_pct": spot_pct,            # bare price move, for the curious
            "dsl": dsl_info,
        })
    return rows


# Preference order when the same fill is reported by multiple sources: the
# reconciled record (events.jsonl) is richest; the AI audit row is poorest.
_CLOSE_SOURCE_RANK = {"reconcile": 0, "external": 1, "dsl": 2, "manual": 3, "ai": 4}


def _find_open_side(events: List[Dict[str, Any]], coin: str, before_idx: int) -> Optional[str]:
    """Walk back through session events to the open (execute) for `coin`."""
    for j in range(before_idx - 1, -1, -1):
        pe = events[j]
        if pe.get("event") == "execute" and pe.get("coin") == coin:
            return pe.get("side")
    return None


def _estimate_close_leverage(coin: str, cfg_leverage: List[int]) -> int:
    """Estimate leverage for a close event that didn't record it.

    Mirrors executor.py: actual leverage = min(config.leverage, HL per-coin max).
    Not perfectly accurate for old trades (config may have changed), but closer
    than config alone — for most coins HL's cap is the binding one.

    `cfg_leverage` is a one-element memo for the config value (lazily fetched
    once per payload build, like the closure it replaced).
    """
    if not cfg_leverage:
        try:
            cfg_leverage.append(int(cfg_get("leverage")))
        except Exception as e:
            # R12-B1: previous code was a real bug — the except branch
            # *re-called* the same `cfg_get("leverage")` line that just
            # raised, so the fallback was guaranteed to fail in the
            # exact same way. The silent swallow then made the bad
            # config invisible. Now: log the read failure, fall back to
            # the historical hard-coded default (10× — the same value
            # the config schema uses when none is set).
            logger.warning(
                "[dashboard] cfg_get('leverage') failed, "
                "falling back to hardcode 10: %s: %s",
                type(e).__name__, e,
            )
            cfg_leverage.append(10)
    cfg = cfg_leverage[0]
    coin_max = _load_max_lev_table().get(coin, 0)
    return min(cfg, coin_max) if coin_max else cfg


def _safe_live_positions_for_llm() -> List[Dict[str, Any]]:
    """R12-B1 thin helper around the LLM-context `_live_positions()` call
    that previously used `except Exception: open_pos = []`. Same fallback
    ([]), now logged so an empty book going to the LLM is visible to the
    operator. The production site in the LLM-context build still calls
    this helper instead of the raw `_live_positions()` for observability.
    """
    try:
        return list(_live_positions() or [])
    except Exception as e:
        logger.warning(
            "[dashboard] _live_positions failed for LLM context "
            "build, using empty: %s: %s",
            type(e).__name__, e,
        )
        return []


def _close_side_and_leverage(
    e: Dict[str, Any],
    events: List[Dict[str, Any]],
    idx: int,
    estimate_leverage: Callable[[str], int],
) -> Tuple[str, int, bool]:
    """Resolve (side, leverage, leverage_estimated) for a session-log close event.

    Newer closes carry `side`/`leverage`; older ones don't, so walk back to the
    matching execute event for side and estimate leverage from live config.
    """
    coin = e.get("coin", "?")
    side = e.get("side") or _find_open_side(events, coin, idx) or "?"
    has_explicit_lev = e.get("leverage") is not None
    leverage = int(e["leverage"]) if has_explicit_lev else estimate_leverage(coin)
    return side, leverage, not has_explicit_lev


def _close_row(
    *,
    ts: Any,
    coin: str,
    source: str,
    side: str,
    leverage: int,
    leverage_estimated: bool,
    reason: Any,
    pnl_pct: Any,
    pnl_pct_gross: Any,
    pnl_source: str,
    fees_pct: Any,
    spot_pct: Any,
    fill_px: Any,
    entry_px: Any,
    executed: bool,
    detail: Any,
) -> Dict[str, Any]:
    """A single closed-trade row; field set is identical across all sources."""
    return {
        "ts": ts,
        "coin": coin,
        "source": source,
        "side": side,
        "leverage": leverage,
        "leverage_estimated": leverage_estimated,
        "reason": reason,
        "pnl_pct": pnl_pct,
        "pnl_pct_gross": pnl_pct_gross,
        "pnl_source": pnl_source,  # "fill" = exact, "estimated"/"unknown" = not
        "fees_pct": fees_pct,
        "spot_pct": spot_pct,
        "fill_px": fill_px,
        "entry_px": entry_px,
        "executed": executed,
        "detail": detail,
    }


def _row_from_dsl_exit(
    e: Dict[str, Any], events: List[Dict[str, Any]], idx: int,
    estimate_leverage: Callable[[str], int],
) -> Dict[str, Any]:
    coin = e.get("coin", "?")
    side, leverage, leverage_estimated = _close_side_and_leverage(e, events, idx, estimate_leverage)

    # If the close logged an actual fill price, use the realized PnL — it matches
    # HL exactly. Otherwise estimate from the DSL trigger mark and subtract
    # round-trip taker fees.
    if e.get("realized_pnl_pct") is not None:
        spot_pct = float(e.get("realized_spot_pct") or 0)
        net_pnl_pct = float(e["realized_pnl_pct"])
        gross_pnl_pct = spot_pct * leverage
        fees_pct = float(e.get("fees_pct") or (HL_TAKER_FEE_PCT * HL_ROUND_TRIP_FILLS * leverage))
        pnl_source = "fill"
    else:
        spot_pct = float(e.get("unrealized_pct", 0) or 0)
        gross_pnl_pct = (float(e["leveraged_pct"]) if e.get("leveraged_pct") is not None
                         else spot_pct * leverage)
        fees_pct = HL_TAKER_FEE_PCT * HL_ROUND_TRIP_FILLS * leverage
        net_pnl_pct = gross_pnl_pct - fees_pct
        pnl_source = "estimated"

    return _close_row(
        ts=e.get("ts"), coin=coin, source="dsl", side=side,
        leverage=leverage, leverage_estimated=leverage_estimated,
        reason=e.get("reason", ""), pnl_pct=net_pnl_pct,
        pnl_pct_gross=gross_pnl_pct, pnl_source=pnl_source, fees_pct=fees_pct,
        spot_pct=spot_pct, fill_px=e.get("fill_px"), entry_px=e.get("entry_px"),
        executed=bool(e.get("executed")), detail=e.get("detail"),
    )


def _row_from_close_position(
    e: Dict[str, Any], events: List[Dict[str, Any]], idx: int,
    estimate_leverage: Callable[[str], int],
) -> Dict[str, Any]:
    coin = e.get("coin", "?")
    side, leverage, leverage_estimated = _close_side_and_leverage(e, events, idx, estimate_leverage)
    # The manual-close endpoint may attach realized fill data
    # (entry_px/fill_px or realized_pnl_pct); use it when present.
    entry_px = e.get("entry_px")
    fill_px = e.get("fill_px") or e.get("exit_px")
    realized_pct = e.get("realized_pnl_pct")
    if realized_pct is not None:
        spot_pct = float(e.get("realized_spot_pct")
                         or e.get("spot_pct") or 0)
        net_pnl_pct = float(realized_pct)
        gross_pnl_pct = spot_pct * leverage
        fees_pct = float(e.get("fees_pct")
                         or (HL_TAKER_FEE_PCT * HL_ROUND_TRIP_FILLS * leverage))
        pnl_source = "fill"
    elif entry_px and fill_px and float(entry_px) > 0:
        spot_pct = (float(fill_px) - float(entry_px)) / float(entry_px) * 100.0
        if side == "short":
            spot_pct = -spot_pct
        gross_pnl_pct = spot_pct * leverage
        fees_pct = HL_TAKER_FEE_PCT * HL_ROUND_TRIP_FILLS * leverage
        net_pnl_pct = gross_pnl_pct - fees_pct
        pnl_source = "fill"
    else:
        spot_pct = 0.0
        gross_pnl_pct = 0.0
        fees_pct = 0.0
        net_pnl_pct = 0.0
        pnl_source = "unknown"
    return _close_row(
        ts=e.get("ts"), coin=coin, source="manual", side=side,
        leverage=leverage, leverage_estimated=leverage_estimated,
        reason=e.get("reason") or "manual_close", pnl_pct=net_pnl_pct,
        pnl_pct_gross=gross_pnl_pct, pnl_source=pnl_source, fees_pct=fees_pct,
        spot_pct=spot_pct, fill_px=fill_px, entry_px=entry_px,
        executed=bool(e.get("ok", e.get("executed"))), detail=e.get("detail"),
    )


def _row_from_external_close(
    e: Dict[str, Any], events: List[Dict[str, Any]], idx: int,
    estimate_leverage: Callable[[str], int],
) -> Dict[str, Any]:
    # Exchange-side close (stop/TP fired off-box). The reconciler also emits a
    # dsl_exit mirror; this branch is kept so older logs that only contain
    # external_close_recorded are still surfaced, and the merge step below
    # de-duplicates against the mirror.
    coin = e.get("coin", "?")
    side, leverage, leverage_estimated = _close_side_and_leverage(e, events, idx, estimate_leverage)
    entry_px = e.get("entry_px")
    exit_px = e.get("exit_px")
    realized_usd = e.get("realized_pnl_usd")
    spot_pct = float(e.get("spot_pct") or 0)
    if realized_usd is not None and entry_px and exit_px:
        size_coin = abs(float(realized_usd)
                        / max(abs(exit_px - entry_px), 1e-12))
        notional = size_coin * float(entry_px)
        if notional > 0:
            net_pnl_pct = float(realized_usd) / notional * 100.0 * leverage
        else:
            net_pnl_pct = spot_pct * leverage
        gross_pnl_pct = spot_pct * leverage
        fees_pct = gross_pnl_pct - net_pnl_pct
        pnl_source = "fill"
    else:
        gross_pnl_pct = spot_pct * leverage
        fees_pct = HL_TAKER_FEE_PCT * HL_ROUND_TRIP_FILLS * leverage
        net_pnl_pct = gross_pnl_pct - fees_pct
        pnl_source = "estimated"
    return _close_row(
        ts=e.get("ts"), coin=coin, source="external", side=side,
        leverage=leverage, leverage_estimated=leverage_estimated,
        reason="exchange_trigger", pnl_pct=net_pnl_pct,
        pnl_pct_gross=gross_pnl_pct, pnl_source=pnl_source, fees_pct=fees_pct,
        spot_pct=spot_pct, fill_px=exit_px, entry_px=entry_px,
        executed=True,
        detail=f"oid={e.get('oid')}" if e.get("oid") else None,
    )


def _row_from_ai_close(
    e: Dict[str, Any], events: List[Dict[str, Any]], idx: int,
    estimate_leverage: Callable[[str], int],
) -> Dict[str, Any]:
    # AI verdict close. The event does not carry fill prices/PnL (the actual
    # order result is in close_position_market's return value), so record an
    # audit row with pnl_source="unknown" rather than a misleading 0% that
    # looks like a breakeven trade.
    coin = e.get("coin", "?")
    side = e.get("side") or _find_open_side(events, coin, idx) or "?"
    leverage = estimate_leverage(coin)
    return _close_row(
        ts=e.get("ts"), coin=coin, source="ai", side=side,
        leverage=leverage, leverage_estimated=True,
        reason=(e.get("reasoning") or "ai_close")[:120],
        pnl_pct=None, pnl_pct_gross=None, pnl_source="unknown",
        fees_pct=None, spot_pct=None, fill_px=None, entry_px=None,
        executed=bool(e.get("executed")), detail=e.get("detail"),
    )


def _row_from_outcome_close(
    rec: Dict[str, Any], estimate_leverage: Callable[[str], int],
) -> Dict[str, Any]:
    # Authoritative reconciled `close` record from events.jsonl — richest fields.
    p = rec.get("payload") or {}
    coin = p.get("coin", "?")
    ts = p.get("closed_at") or _iso_to_ms(rec.get("timestamp"))
    leverage = int(p.get("leverage") or 0) or estimate_leverage(coin)
    entry_px = p.get("entry_px")
    fill_px = p.get("exit_px")
    realized_pct = p.get("realized_pnl_pct")
    spot_pct = float(p.get("spot_pct") or 0)
    if realized_pct is not None:
        net_pnl_pct = float(realized_pct)
        gross_pnl_pct = spot_pct * leverage if leverage else 0.0
        notional = float(p.get("notional_usd") or 0)
        fee_usd = float(p.get("fee_usd") or 0)
        if notional > 0 and leverage:
            fees_pct = fee_usd / notional * 100.0 * leverage
        else:
            fees_pct = gross_pnl_pct - net_pnl_pct
        pnl_source = "fill"
    else:
        gross_pnl_pct = spot_pct * leverage if leverage else 0.0
        fees_pct = HL_TAKER_FEE_PCT * HL_ROUND_TRIP_FILLS * leverage if leverage else 0.0
        net_pnl_pct = gross_pnl_pct - fees_pct
        pnl_source = "estimated"
    close_source = p.get("close_source") or "reconcile"
    return _close_row(
        ts=ts, coin=coin, source="reconcile",
        side=p.get("side") or "?", leverage=leverage,
        leverage_estimated=p.get("leverage") is None,
        reason=close_source, pnl_pct=net_pnl_pct,
        pnl_pct_gross=gross_pnl_pct, pnl_source=pnl_source, fees_pct=fees_pct,
        spot_pct=spot_pct, fill_px=fill_px, entry_px=entry_px,
        executed=True,
        detail=f"hold={p.get('hold_minutes')}m oid={p.get('close_oid')}"
               if p.get("hold_minutes") is not None else None,
    )


# Session-log close-event parsers, keyed by event name.
_SESSION_CLOSE_PARSERS: Dict[str, Callable] = {
    "dsl_exit": _row_from_dsl_exit,
    "close_position": _row_from_close_position,
    "external_close_recorded": _row_from_external_close,
    "ai_close": _row_from_ai_close,
}


def _deduplicate_close_rows(rows: List[Dict[str, Any]], window_ms: int) -> List[Dict[str, Any]]:
    """Merge cross-source duplicates of one fill; newest-first.

    A single fill can be reported by MULTIPLE sources (a dsl_exit backfill + its
    external_close_recorded mirror, or an events.jsonl `close`) — merge only
    those cross-source duplicates for the same coin AND side within the window.
    Two rows from the SAME source are always distinct trades (a greedy
    nearest-window match here would chain several rapid sequential closes into
    one, F2/limit bug). Prefer the richest row (rank in
    ``_CLOSE_SOURCE_RANK``: `reconcile` > `external` > `dsl` > `manual` > `ai`).
    """
    rows.sort(key=lambda r: (
        -(r.get("ts") or 0),
        _CLOSE_SOURCE_RANK.get(r.get("source"), 9),
    ))
    merged: List[Dict[str, Any]] = []
    for r in rows:
        dup = None
        r_side = (r.get("side") or "").lower() or None
        for m in merged:
            m_side = (m.get("side") or "").lower() or None
            if (m.get("source") != r.get("source")
                    and m.get("coin") == r.get("coin")
                    and r_side == m_side
                    and m.get("ts") and r.get("ts")
                    and abs(int(m["ts"]) - int(r["ts"])) <= window_ms):
                dup = m
                break
        if dup is None:
            merged.append(r)
        elif _CLOSE_SOURCE_RANK.get(r.get("source"), 9) < _CLOSE_SOURCE_RANK.get(dup.get("source"), 9):
            merged.remove(dup)
            merged.append(r)

    merged.sort(key=lambda r: -(r.get("ts") or 0))
    return merged


def _closed_trades_payload(limit: int = 20) -> List[Dict[str, Any]]:
    """Walk both the session log and the outcome log for close events.

    Recognised events:
      - ``dsl_exit`` (session-log) — DSL/trailing-stop exits, full PnL when the
        close logged an actual fill, otherwise estimated.
      - ``close_position`` (session-log) — operator manual close from the UI.
      - ``external_close_recorded`` (session-log) — exchange-side closes detected
        by the reconciler (stop/take-profit fired on the exchange).
      - ``close`` (events.jsonl) — authoritative reconciled close records with
        the richest fields (realized PnL, fees, hold time, close source).
      - ``ai_close`` (session-log) — AI-driven close; usually lacks price/PnL
        fields so it is recorded as a zero/unknown row for audit visibility.

    Events describing the same fill (a ``dsl_exit`` backfill and its
    ``external_close_recorded`` sibling, or an events.jsonl ``close``) are merged
    so the UI never lists one position exit twice. Returns newest-first.

    Each row carries:
      - `spot_pct`: raw price-move %. This is what the DSL engine measures
        and what HL would show you as "unrealized PnL %" on the position.
      - `pnl_pct`: leveraged margin PnL — what shows up in the HL P&L view.
        Equals spot_pct × leverage.
      - `side` and `leverage`: pulled from the event itself for new closes;
        for older events lacking those fields, walked back to the matching
        execute event (for side) and the live config (for leverage).
    """
    events = _read_log_lines()
    cfg_leverage: List[int] = []  # lazy-fetched fallback memo

    def _estimate_leverage(coin: str) -> int:
        return _estimate_close_leverage(coin, cfg_leverage)

    rows: List[Dict[str, Any]] = []

    # ── session-log: dsl_exit / close_position / external_close_recorded / ai_close ──
    for i in range(len(events) - 1, -1, -1):
        parser = _SESSION_CLOSE_PARSERS.get(events[i].get("event"))
        if parser is not None:
            rows.append(parser(events[i], events, i, _estimate_leverage))

    # ── events.jsonl: authoritative reconciled `close` records ──
    for rec in _read_outcome_lines():
        if rec.get("event") == "close":
            rows.append(_row_from_outcome_close(rec, _estimate_leverage))

    # ── De-duplicate cross-source reports of the same fill (see helper) ──
    dedup_window_ms = int(os.environ.get("HERMES_CLOSED_TRADES_DEDUP_MS", "5000"))
    return _deduplicate_close_rows(rows, dedup_window_ms)[:limit]


# A heartbeat that momentarily failed to fetch a HIP-3 dex reports equity far
# below trend (main-dex-only, e.g. $88 vs the real $220 aggregate). Capped
# positions can't lose tens of % in one 60s tick, so a point far below the
# TRAILING median of accepted points is *probably* a bad read — but a genuine
# flash-crash/liquidation is indistinguishable here. F1: keep the point and flag
# it rather than silently dropping it, so a real drawdown stays visible.
_EQUITY_DIP_RATIO = float(os.environ.get("HERMES_EQUITY_DIP_RATIO", "0.7"))
_EQUITY_DIP_WINDOW = int(os.environ.get("HERMES_EQUITY_DIP_WINDOW", "15"))


def _equity_curve_payload(range_s: int) -> List[Dict[str, Any]]:
    """Series of (ts, equity) points from loop_heartbeat events within `range_s`.

    Each point carries ``flag``: "ok" or "degraded". A point far below the
    trailing median of accepted points (``_EQUITY_DIP_RATIO``) is *flagged* as a
    likely PARTIAL-DEX degraded read — it is still returned so a genuine
    flash-crash is never hidden, but the UI may render it dashed/greyed and it
    does not feed the trailing reference window. Using the *trailing* (not
    global) median preserves genuine gradual growth across the window.
    """
    from statistics import median

    cutoff = int(time.time() * 1000) - range_s * 1000
    raw: List[tuple] = []
    for e in _read_log_lines():
        if e.get("event") != "loop_heartbeat":
            continue
        if e.get("ts", 0) < cutoff:
            continue
        eq = float(e.get("equity", 0) or 0)
        if eq <= 0:
            continue
        raw.append((e["ts"], eq))

    series: List[Dict[str, Any]] = []
    window: List[float] = []  # last N accepted equities (trailing reference)
    for ts, eq in raw:
        ref = median(window) if window else eq
        degraded = bool(window) and eq < _EQUITY_DIP_RATIO * ref
        series.append({"ts": ts, "equity": round(eq, 2),
                       "flag": "degraded" if degraded else "ok"})
        if not degraded:
            window.append(eq)
            if len(window) > _EQUITY_DIP_WINDOW:
                window.pop(0)
    return series


# ── SSE feed ─────────────────────────────────────────────────────────────────

# F26: lines replayed to a fresh SSE connection, and the keepalive interval that
# stops proxies (nginx/Cloudflare) from closing an idle stream.
_SSE_REPLAY_LINES = int(os.environ.get("HERMES_SSE_REPLAY_LINES", "500"))
_SSE_HEARTBEAT_S = float(os.environ.get("HERMES_SSE_HEARTBEAT_S", "15"))


async def _tail_log_sse() -> AsyncIterator[str]:
    """Stream new session-log lines as SSE events. Replays the recent past first."""
    # Replay buffer so a fresh connection sees the recent past, not just future events.
    # session_log.tail() reads backward from end of file in chunks; offload to a
    # thread so it doesn't block the event loop for other routes / SSE clients.
    replay = await asyncio.to_thread(session_log.tail, _SSE_REPLAY_LINES)
    for e in replay:
        yield f"data: {json.dumps(e)}\n\n"

    def _stat_size() -> int:
        return _LOG_PATH.stat().st_size if _LOG_PATH.exists() else 0

    last_size = await asyncio.to_thread(_stat_size)
    # Heartbeat every _SSE_HEARTBEAT_S keeps proxies (nginx, Cloudflare) from
    # closing idle SSE.
    last_heartbeat = time.time()

    def _read_new_lines(prev_size: int) -> tuple[list[str], int]:
        """Read & validate new lines appended since prev_size. Runs in a thread
        because open()/read() on a multi-MB log would block the event loop."""
        # F21: log rotation races this poll — exists()/stat()/open() can each
        # raise in the instant the old file is renamed and the new one created.
        # Treat that transient window as "nothing new this tick" and keep the
        # stream alive; the next poll re-detects the file (size resets to 0).
        try:
            if not _LOG_PATH.exists():
                return [], prev_size
            cur = _LOG_PATH.stat().st_size
            if cur < prev_size:
                prev_size = 0  # file rotated
            if cur <= prev_size:
                return [], cur
            out: list[str] = []
            with _LOG_PATH.open() as f:
                f.seek(prev_size)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    out.append(line)
            return out, cur
        except (FileNotFoundError, PermissionError, OSError) as e:
            logger.debug("SSE tail: log unreadable this tick (%s); will retry", e)
            return [], prev_size

    while True:
        await asyncio.sleep(1.0)
        # F21: belt-and-suspenders — even if an unexpected error escapes the
        # thread worker, the SSE generator must not die (one raised exception
        # permanently kills the stream for that client).
        try:
            new_lines, new_size = await asyncio.to_thread(_read_new_lines, last_size)
        except Exception as e:
            logger.warning("SSE tail: poll failed, keeping stream alive: %s", e)
            continue
        if new_size < last_size:
            last_size = 0  # rotated
        if new_lines:
            for line in new_lines:
                yield f"data: {line}\n\n"
            last_size = new_size
        elif new_size != last_size:
            last_size = new_size

        if time.time() - last_heartbeat > _SSE_HEARTBEAT_S:
            yield ": keepalive\n\n"
            last_heartbeat = time.time()


# ── operator gate ────────────────────────────────────────────────────────────

# Brute-force protection for the operator surface (F11). Failures are counted
# per client IP inside this process; after _AUTH_MAX_FAILURES consecutive
# failures the IP is locked out for _AUTH_COOLDOWN_SEC (429 + Retry-After).
# A successful auth clears the counter. Tunable via env; XFF is trusted only
# when HERMES_TRUST_PROXY=1 (direct socket peer otherwise — spoofable XFF must
# not let an attacker rotate its own key).
_AUTH_MAX_FAILURES = int(os.environ.get("HERMES_AUTH_MAX_FAILURES", "5"))
_AUTH_COOLDOWN_SEC = int(os.environ.get("HERMES_AUTH_COOLDOWN_SEC", str(5 * 60)))
_AUTH_TRUST_PROXY = os.environ.get("HERMES_TRUST_PROXY", "") == "1"
_auth_failures: Dict[str, Dict[str, float]] = {}
_auth_lock = threading.Lock()

# F11: per-IP rate limit on AUTHENTICATED operator WRITES. The failure lockout
# above only stops token guessing; a valid-but-abused token (leaked secret, a
# runaway script) could otherwise fire close/mode/config writes with no ceiling.
# Sliding window: at most _WRITE_RATE_MAX state-changing requests per
# _WRITE_RATE_WINDOW_S from one IP (429 + Retry-After past the cap). Reads are
# not limited. In-process state, same lifecycle as _auth_failures. Set
# HERMES_OP_WRITE_RATE_MAX=0 to disable.
_WRITE_RATE_MAX = int(os.environ.get("HERMES_OP_WRITE_RATE_MAX", "30"))
_WRITE_RATE_WINDOW_S = float(os.environ.get("HERMES_OP_WRITE_RATE_WINDOW_S", "60"))
_write_hits: Dict[str, List[float]] = {}


def _check_operator_write_rate(ip: str) -> None:
    """Raise 429 if ``ip`` exceeds the authenticated write-rate window (F11)."""
    if _WRITE_RATE_MAX <= 0:
        return
    now = time.monotonic()
    cutoff = now - _WRITE_RATE_WINDOW_S
    with _auth_lock:
        hits = [t for t in _write_hits.get(ip, []) if t > cutoff]
        if len(hits) >= _WRITE_RATE_MAX:
            retry = _WRITE_RATE_WINDOW_S - (now - hits[0])
            raise HTTPException(
                status_code=429,
                detail="operator write rate limit exceeded; slow down",
                headers={"Retry-After": str(max(1, int(retry)))},
            )
        hits.append(now)
        _write_hits[ip] = hits
        # Opportunistic sweep so idle IPs don't leak timestamps forever.
        if len(_write_hits) > 1024:
            for k in [k for k, v in _write_hits.items()
                      if not v or v[-1] <= cutoff]:
                _write_hits.pop(k, None)


def _client_ip(request: Request) -> str:
    if _AUTH_TRUST_PROXY:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _extract_bearer(request: Request) -> str:
    """Return the bearer token from ``Authorization: Bearer <token>`` or ``''``."""
    auth = request.headers.get("Authorization") or request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return ""


def _require_operator(request: Request, write: bool = False) -> None:
    """401 unless a valid operator token is supplied (429 while rate-limited).

    Accepted token transports (in priority order):
      1. Standard ``Authorization: Bearer <token>`` header (preferred)
      2. Legacy ``X-Operator-Token`` header

    The ``?token=`` query-parameter transport was removed (P1-12): query strings
    land in access logs, browser history, and proxy logs, leaking the secret.
    Comparison uses hmac.compare_digest (constant-time, no timing oracle).

    Checking at request time (not import time) means rotating the token doesn't
    need a restart. Missing env var = operator surface is closed.
    """
    expected = os.environ.get("HERMES_OPERATOR_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="operator surface disabled (set HERMES_OPERATOR_TOKEN)")

    ip = _client_ip(request)
    now = time.time()
    with _auth_lock:
        rec = _auth_failures.get(ip)
        if rec and now < rec.get("locked_until", 0.0):
            retry = int(rec["locked_until"] - now) + 1
            raise HTTPException(
                status_code=429,
                detail="too many failed operator auth attempts — try again later",
                headers={"Retry-After": str(retry)},
            )

    provided = (
        _extract_bearer(request)
        or request.headers.get("X-Operator-Token", "")
    )
    # P1-12: constant-time comparison; never accept a ?token= query param.
    if not hmac.compare_digest(provided or "", expected):
        with _auth_lock:
            rec = _auth_failures.get(ip) or {"fails": 0, "locked_until": 0.0}
            rec["fails"] = int(rec.get("fails", 0)) + 1
            locked_until = 0.0
            if rec["fails"] >= _AUTH_MAX_FAILURES:
                locked_until = now + _AUTH_COOLDOWN_SEC
                rec["locked_until"] = locked_until
                rec["fails"] = 0
            _auth_failures[ip] = rec
        logger.warning(
            "[operator] auth failed from %s (consecutive failures, cooldown active: %s)",
            ip, bool(locked_until),
        )
        if locked_until:
            raise HTTPException(
                status_code=429,
                detail="too many failed operator auth attempts — try again later",
                headers={"Retry-After": str(_AUTH_COOLDOWN_SEC)},
            )
        # Send a WWW-Authenticate challenge so standard HTTP clients know to
        # present a bearer token (P0-2).
        raise HTTPException(
            status_code=401,
            detail="invalid operator token",
            headers={"WWW-Authenticate": 'Bearer realm="hermes-trader"'},
        )

    # Success — clear the failure counter for this IP.
    with _auth_lock:
        _auth_failures.pop(ip, None)
    # F11: state-changing requests are additionally rate-limited per IP even
    # with a valid token (a leaked token / runaway script must not be able to
    # fire unlimited close/mode/config writes).
    if write:
        _check_operator_write_rate(ip)


def require_operator_write(request: Request) -> None:
    """P0-7: the canonical dependency for STATE-CHANGING operator endpoints.

    Thin wrapper that pins ``write=True`` on ``_require_operator`` so the
    per-IP F11 write rate-limit always fires — no developer can forget to
    pass ``write=True`` in a route decorator. Read-only routes continue to
    use ``_require_operator`` directly (with the default ``write=False``).

    Always authenticate with a valid ``HERMES_OPERATOR_TOKEN``; 401 on bad
    / missing token, 429 on auth-failure lockout, 429 on write rate-limit
    exhaustion, 503 on operator surface disabled.
    """
    _require_operator(request, write=True)


def _log_operator_action(action: str, *, via: str, result: Optional[Dict[str, Any]] = None,
                         **extra: Any) -> None:
    """F22: persist a human-driven operator action to the session log.

    Forked into the authoritative events.jsonl (``operator_action`` whitelist)
    so manual closes / kills survive restarts and are attributable. Best-effort
    like every session-log write; a disk failure must never block the action.
    """
    record: Dict[str, Any] = {
        "event": "operator_action",
        "ts": int(time.time() * 1000),
        "action": action,
        "via": via,
    }
    if extra:
        record.update(extra)
    if result is not None:
        record["result"] = {
            k: result.get(k) for k in (
                "ok", "executed", "noop", "error", "side",
                "fill_px", "realized_pnl_pct", "leverage",
            ) if k in result
        }
    try:
        session_log.append(record)
    except Exception:  # pragma: no cover - audit must never break the action
        pass


# ── agent-config write validation (F25: hoisted from register_routes so the ──
# ── web config API, the schema endpoint, and the terminal `set` handler all ──
# ── share the same whitelist / type / range gate) ───────────────────────────

# F27: the whitelist / type / range gate used to be two hand-kept tables
# (_CONFIG_TYPES + _CONFIG_RANGES) here. It now lives in
# agents/config_schema.py as a Pydantic model (single source of truth shared
# with the CLI and the legacy merge endpoint); _validate_config_updates and
# _coerce_config_value are imported from there at the top of this module.


def _config_apply(updates: Dict[str, Any], backup: bool = False) -> Dict[str, Any]:
    """F20: atomic read-modify-write of `.agent-config.json`. Takes the
    process-wide threading lock (cheap, avoids flock contention between
    dashboard threads) and then performs the RMW inside
    :func:`update_agent_config`, whose exclusive flock serializes against
    other *processes* (CLI/daemon) too. Returns ``{"old": <key: prior value>,
    "new": <key: applied value>}``; an unset key's prior value is reported as
    None (web API) or ``"<unset>"`` by callers that render it as text."""
    with _config_rmw_lock:
        with update_agent_config(backup=backup) as cfg:
            old_snapshot = {k: cfg.get(k) for k in updates}
            cfg.update(updates)
        return {"old": old_snapshot, "new": {k: cfg.get(k) for k in updates}}


# ── terminal command center (F25: each built-in verb is a module-level ──────
# ── async handler; the endpoint dispatches through _TERMINAL_HANDLERS and ───
# ── anything unrecognised falls through to the OpenRouter chat fallback) ────

def _positions_from_state(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract open positions from a fetch_account_state payload.

    Each row carries coin / side / size / entry / szi / uPnL so the five
    terminal consumers (close-bulk, positions, kill, dump, LLM context) share
    one extraction instead of five hand-rolled list comprehensions. F24: the
    per-record normalization now comes from _parse_raw_position (shared with
    the dashboard view rows); malformed entries are skipped rather than
    raising and aborting the whole list.
    """
    rows: List[Dict[str, Any]] = []
    for p in state.get("asset_positions") or []:
        parsed = _parse_raw_position(p.get("position", {}))
        if parsed is None:
            continue
        rows.append({
            "coin": parsed["coin"],
            "side": parsed["side"],
            "size": abs(parsed["szi"]),
            "entry": parsed["entry"],
            "szi": parsed["szi"],
            "uPnL": parsed["unrealized_usd"],
        })
    return rows


def _live_positions() -> List[Dict[str, Any]]:
    """Fetch live open positions (main-dex + HIP-3). Raises on read failure;
    callers decide whether that is fatal (bulk close / kill) or informational."""
    user = resolve_user_address()
    state = fetch_account_state(user, include_hip3=True) if user else {}
    return _positions_from_state(state)


async def _h_help(parts: List[str], cmd: str) -> JSONResponse:
    return JSONResponse({"response": (
        "commands:\n"
        "  status                — equity, daily PnL, open, tick, scan triggers\n"
        "  positions             — live positions w/ uPnL (winners + losers grouped)\n"
        "  trades [n]            — last n real fills from memory (default 10)\n"
        "  config                — dump current .agent-config.json\n"
        "  dump                  — full state (config + positions + last events)\n"
        "  regime                — cached regime per proxy\n"
        "  pause / resume / shadow — flip mode OFF / LIVE / SHADOW\n"
        "  close <coin>          — market-close a single position\n"
        "  close all             — market-close every open position\n"
        "  close losing          — market-close every position with uPnL < 0\n"
        "  close winning         — market-close every position with uPnL > 0\n"
        "  set <key> <value>     — update .agent-config.json (int/float/bool/str inferred)\n"
        "  kill                  — pause trading then close all (panic button)\n"
        "  help                  — this list. anything else → ask the chat model"
    ), "kind": "help"})


async def _h_status(parts: List[str], cmd: str) -> JSONResponse:
    try:
        events = session_log.tail(50) or []
        last_hb = next((e for e in reversed(events) if e.get("event") == "loop_heartbeat"), {})
        last_scan = next((e for e in reversed(events) if e.get("event") == "scan"), {})
        age_s = max(0, int(time.time() - (last_hb.get("ts", 0) / 1000))) if last_hb else None
        msg = (f"equity ${last_hb.get('equity', 0):.2f}  "
               f"daily {last_hb.get('daily_pnl', 0):+.2f}  "
               f"open {last_hb.get('open_positions', 0)}  "
               f"tick {age_s}s ago  "
               f"last scan: {last_scan.get('triggers', 0)} triggers")
        return JSONResponse({"response": msg, "kind": "status"})
    except Exception as e:
        return JSONResponse({"response": f"status read failed: {e}", "kind": "error"})


async def _h_pause_resume(parts: List[str], cmd: str) -> JSONResponse:
    new_mode = "OFF" if parts[0].lower() == "pause" else "LIVE"
    result = await asyncio.to_thread(_config_apply, {"mode": new_mode})
    old = result["old"].get("mode", "?")
    # F22: a terminal pause/resume is the same high-value mode switch as the
    # web path — persist it so it survives restarts (forked to events.jsonl).
    try:
        session_log.append({
            "event": "mode_switch",
            "ts": int(time.time() * 1000),
            "old": old,
            "new": new_mode,
            "via": "terminal",
        })
    except Exception as e:
        # R12-B1: this is the **audit trail** for a mode flip. A silent
        # pass here means the operator paused/resumed the loop but the
        # event is gone — exactly the kind of "who did what" gap that
        # makes a post-mortem impossible. Warning, not debug: the
        # handler still returned 200, but the audit row is missing.
        logger.warning(
            "[terminal] session_log.append mode_switch failed "
            "for pause/resume %s → %s: %s: %s",
            old, new_mode, type(e).__name__, e,
        )
    return JSONResponse({"response": f"mode {old} → {new_mode}", "kind": "action"})


async def _h_shadow(parts: List[str], cmd: str) -> JSONResponse:
    # SHADOW = shadow book: full pipeline, no real fills. Audited like
    # pause/resume (F22) since it is the same high-value mode switch.
    new_mode = "SHADOW"
    result = await asyncio.to_thread(_config_apply, {"mode": new_mode})
    old = result["old"].get("mode", "?")
    try:
        session_log.append({
            "event": "mode_switch",
            "ts": int(time.time() * 1000),
            "old": old,
            "new": new_mode,
            "via": "terminal",
        })
    except Exception as e:
        # R12-B1: same as pause/resume — losing the audit row for a
        # SHADOW entry is how "we never went to shadow" arguments start
        # in post-mortems. Warning, the handler still returns 200.
        logger.warning(
            "[terminal] session_log.append mode_switch failed "
            "for shadow %s → SHADOW: %s: %s",
            old, type(e).__name__, e,
        )
    return JSONResponse(
        {"response": f"mode {old} → {new_mode} (shadow: no real orders)",
         "kind": "action"})


async def _h_close(parts: List[str], cmd: str) -> Optional[JSONResponse]:
    # `close` with no argument falls through to the LLM fallback.
    if len(parts) < 2:
        return None
    from hermes_trader.agents.executor import close_position_market
    target = parts[1].lower()
    if target in ("all", "losing", "winning"):
        # Bulk close — iterate live positions, filter, close each.
        try:
            # include_hip3=True so `close all` also closes xyz:/vntl:/...
            # positions, not just main-dex.
            open_pos = _live_positions()
        except Exception as e:
            return JSONResponse({"response": f"could not read live positions: {e}", "kind": "error"})

        if target == "losing":
            targets = [p for p in open_pos if p["uPnL"] < 0]
        elif target == "winning":
            targets = [p for p in open_pos if p["uPnL"] > 0]
        else:  # all
            targets = open_pos

        if not targets:
            return JSONResponse({"response": f"no positions matched `close {target}`", "kind": "info"})

        results = []
        closed = 0
        for p in targets:
            coin = p["coin"]
            try:
                r = await asyncio.to_thread(close_position_market, coin)
                ok = bool(r.get("ok") or r.get("executed"))
                closed += 1 if ok else 0
                results.append(f"  {coin:<14} {('✓' if ok else '✗')} uPnL={p['uPnL']:+.2f}")
            except Exception as e:
                r = {"ok": False, "error": str(e)}
                results.append(f"  {coin:<14} ✗ {e}")
            # F22: audit every manual close (per coin, incl. failures).
            _log_operator_action(
                "close", via="terminal", coin=coin, bulk=target, result=r,
            )
        # F22: one summary row for the bulk action itself.
        _log_operator_action(
            "close_bulk", via="terminal", bulk=target,
            count=len(targets), closed=closed,
        )
        head = f"closed {len(targets)} position(s) [{target}]:\n"
        return JSONResponse({"response": head + "\n".join(results), "kind": "action"})

    # Single-coin close (preserve original behavior)
    coin = parts[1] if ":" in parts[1] else parts[1].upper()
    result = await asyncio.to_thread(close_position_market, coin)
    # F22: audit the manual close (no-op/already-flat included so attempted
    # operator actions are traceable).
    _log_operator_action("close", via="terminal", coin=coin, result=result)
    logger.info("manual close via terminal: %s -> %s", coin,
                {k: result.get(k) for k in ("ok", "noop", "error") if k in result})
    return JSONResponse({"response": f"close {coin}: {result}", "kind": "action"})


async def _h_positions(parts: List[str], cmd: str) -> JSONResponse:
    try:
        rows = _live_positions()
        if not rows:
            return JSONResponse({"response": "no open positions", "kind": "info"})
        rows.sort(key=lambda r: -r["uPnL"])
        lines = [f"  {r['coin']:<14} {r['side']:<5} size={r['size']:>9.4f} entry={r['entry']:<10} uPnL={r['uPnL']:+.2f}"
                 for r in rows]
        total = sum(r["uPnL"] for r in rows)
        head = f"{len(rows)} open · total uPnL ${total:+.2f}\n"
        return JSONResponse({"response": head + "\n".join(lines), "kind": "status"})
    except Exception as e:
        return JSONResponse({"response": f"positions read failed: {e}", "kind": "error"})


async def _h_trades(parts: List[str], cmd: str) -> JSONResponse:
    try:
        from hermes_trader.agents.memory import memory as _mem
        _mem.load()
        n = 10
        if len(parts) >= 2:
            try:
                n = max(1, min(50, int(parts[1])))
            except ValueError:
                pass
        real = [t for t in (_mem.get_recent_trades(50) or []) if float(t.get("size_usd") or 0) > 0]
        last_n = real[-n:]
        if not last_n:
            return JSONResponse({"response": "no real trades in memory yet", "kind": "info"})
        from datetime import datetime
        lines = []
        for t in last_n:
            ts = datetime.fromtimestamp(t["executed_at"]/1000).strftime("%m-%d %H:%M:%S")
            lines.append(f"  {ts}  {t.get('coin'):<14} {t.get('side','?'):<5} "
                         f"entry={t.get('entry_px',0):<10} size=${float(t.get('size_usd') or 0):.2f}")
        return JSONResponse({"response": f"last {len(last_n)} fills:\n" + "\n".join(lines), "kind": "info"})
    except Exception as e:
        return JSONResponse({"response": f"trades read failed: {e}", "kind": "error"})


async def _h_set(parts: List[str], cmd: str) -> Optional[JSONResponse]:
    # `set` with fewer than 3 tokens falls through to the LLM fallback.
    if len(parts) < 3:
        return None
    key = parts[1]
    raw = " ".join(parts[2:]).strip()
    new_val = _coerce_config_value(raw)
    # F4: same whitelist/type/range gate as the web config API —
    # terminal `set` must not be an unvalidated write path.
    errors = _validate_config_updates({key: new_val})
    if new_val is None:
        errors.append(f"{key}: null/none values are not allowed via set")
    if errors:
        return JSONResponse({"response": "rejected: " + "; ".join(errors),
                             "kind": "error"})
    result = await asyncio.to_thread(_config_apply, {key: new_val})
    old_val = result["old"].get(key, "<unset>")
    if old_val is None:
        old_val = "<unset>"
    # F22: a terminal `set` is the same config write as the web API — persist
    # the audit event (forked to events.jsonl) so it survives restarts.
    try:
        session_log.append({
            "event": "config_update",
            "ts": int(time.time() * 1000),
            "updates": {key: new_val},
            "old": {key: old_val},
            "via": "terminal",
        })
    except Exception as e:
        # R12-B1: config_update is the per-key audit trail that complements
        # mode_switch. A silent pass here means `set leverage 25` ran in
        # the live config but no one can tell from the log. Warning level.
        logger.warning(
            "[terminal] session_log.append config_update failed "
            "for %s=%r: %s: %s",
            key, new_val, type(e).__name__, e,
        )
    return JSONResponse({"response": f"config[{key}]: {old_val} → {new_val}  (type={type(new_val).__name__})",
                         "kind": "action"})


async def _h_kill(parts: List[str], cmd: str) -> JSONResponse:
    from hermes_trader.agents.executor import close_position_market
    await asyncio.to_thread(_config_apply, {"mode": "OFF"})
    # F22: kill flips mode to OFF — audit the mode switch like pause/resume.
    try:
        session_log.append({
            "event": "mode_switch",
            "ts": int(time.time() * 1000),
            "old": None,
            "new": "OFF",
            "via": "terminal",
            "reason": "kill",
        })
    except Exception as e:
        # R12-B1: the kill switch audit row is the single most important
        # audit row in the system. Losing it silently is how "the kill
        # switch didn't work" / "no one pressed kill" arguments start.
        # logger.exception (not warning) so the full traceback is
        # preserved — the operator needs to see exactly which I/O path
        # broke the audit so it can be fixed before the next incident.
        logger.exception(
            "[terminal] session_log.append mode_switch failed "
            "for KILL: %s",
            e,
        )
    try:
        open_coins = [p["coin"] for p in _live_positions()]
    except Exception as e:
        _log_operator_action("kill", via="terminal", error=f"position-list fetch failed: {e}")
        return JSONResponse({"response": f"mode → OFF, but position-list fetch failed: {e}", "kind": "error"})
    closed = []
    closed_rows = []
    for c in open_coins:
        try:
            r = await asyncio.to_thread(close_position_market, c)
            ok = bool(r.get("ok") or r.get("executed"))
            closed.append(f"  {c}: {'✓' if ok else '✗'}")
            closed_rows.append({"coin": c, "ok": ok})
            _log_operator_action("close", via="terminal", coin=c, bulk="kill", result=r)
        except Exception as e:
            closed.append(f"  {c}: ✗ {e}")
            closed_rows.append({"coin": c, "ok": False, "error": str(e)})
            _log_operator_action("close", via="terminal", coin=c, bulk="kill",
                                 result={"ok": False, "error": str(e)})
    # F22: one summary row for the kill switch itself.
    _log_operator_action("kill", via="terminal", count=len(open_coins),
                         closed=sum(1 for r in closed_rows if r.get("ok")))
    head = f"KILL · mode → OFF · closed {len(open_coins)} position(s):\n"
    return JSONResponse({"response": head + ("\n".join(closed) if closed else "  (no positions to close)"),
                         "kind": "action"})


async def _h_dump(parts: List[str], cmd: str) -> JSONResponse:
    try:
        user = resolve_user_address()
        state = fetch_account_state(user, include_hip3=True) if user else {}
        events = session_log.tail(10) or []
        snap = {
            "config": read_agent_config(),
            "equity": float(state.get("equity", 0) or 0),
            "open_positions": _positions_from_state(state),
            "recent_events": [{k: v for k, v in e.items() if k != "ts"} for e in events],
        }
        return JSONResponse({"response": json.dumps(snap, indent=2, default=str), "kind": "info"})
    except Exception as e:
        return JSONResponse({"response": f"dump failed: {e}", "kind": "error"})


async def _h_regime(parts: List[str], cmd: str) -> JSONResponse:
    try:
        from hermes_trader.agents.market_regime import regime_snapshot
        snap = regime_snapshot()
        lines = [f"  {p}: {info.get('regime', '?')}  ({int(info.get('age_s', 0))}s old)"
                 for p, info in snap.items()]
        return JSONResponse({"response": "regime snapshot:\n" + "\n".join(lines) if lines else "no cached regimes yet",
                             "kind": "info"})
    except Exception as e:
        return JSONResponse({"response": f"regime fetch failed: {e}", "kind": "error"})


async def _h_config(parts: List[str], cmd: str) -> JSONResponse:
    cfg = read_agent_config()
    return JSONResponse({"response": json.dumps(cfg, indent=2), "kind": "info"})


# Dispatch table for built-in terminal verbs. A handler returning None signals
# "fall through to the LLM chat" (e.g. `close` / `set` without enough args).
_TERMINAL_HANDLERS: Dict[str, Callable[..., Any]] = {
    "help": _h_help,
    "?": _h_help,
    "status": _h_status,
    "pause": _h_pause_resume,
    "resume": _h_pause_resume,
    "shadow": _h_shadow,
    "close": _h_close,
    "positions": _h_positions,
    "trades": _h_trades,
    "set": _h_set,
    "kill": _h_kill,
    "dump": _h_dump,
    "regime": _h_regime,
    "config": _h_config,
}


async def _terminal_llm_chat(cmd: str) -> JSONResponse:
    """LLM fallback (Nous Hermes via OpenRouter), primed with a compact snapshot
    of recent agent state so the chat is grounded in the bot's actual world."""
    try:
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            return JSONResponse({"response": "Hermes chat unavailable: OPENROUTER_API_KEY not set", "kind": "error"})

        # Real trades come from memory (the 100-entry trade ring buffer);
        # the feed supplies recent DSL exits + ta_skips so "why did X close"
        # questions have context.
        from hermes_trader.agents.memory import memory as _mem
        _mem.load()
        events = session_log.tail(80) or []
        last_hb = next((e for e in reversed(events) if e.get("event") == "loop_heartbeat"), {})

        # Last 8 executed trades (size_usd > 0 means it actually placed)
        mem_trades = _mem.get_recent_trades(50) or []
        real_trades = [t for t in mem_trades if float(t.get("size_usd") or 0) > 0][-8:]

        # Open positions from the live exchange state (already maintained
        # by the heartbeat sync); fall back to memory if heartbeat is stale.
        # R12-B1: route through the thin helper so the failure (if any) is
        # logged at the dashboard layer, not swallowed.
        open_pos = _safe_live_positions_for_llm()

        recent_dsl_exits = [e for e in events if e.get("event") == "dsl_exit"][-5:]
        recent_ta_skips = [e for e in events if e.get("event") == "ta_skip"][-5:]
        recent_research = [e for e in events if e.get("event") == "research"][-5:]

        ctx = {
            "equity": last_hb.get("equity"),
            "daily_pnl": last_hb.get("daily_pnl"),
            "open_position_count": last_hb.get("open_positions"),
            "config_snippet": last_hb.get("config", {}),
            "open_positions": open_pos[:20],
            "recent_trades": [
                {
                    "coin": t.get("coin"),
                    "side": t.get("side"),
                    "entry_px": t.get("entry_px"),
                    "size_usd": t.get("size_usd"),
                    "executed_at": t.get("executed_at"),
                } for t in real_trades
            ],
            "recent_dsl_exits": [
                {"coin": e.get("coin"), "reason": e.get("reason"),
                 "pnl_pct": e.get("realized_pnl_pct") or e.get("unrealized_pct"),
                 "ts": e.get("ts")}
                for e in recent_dsl_exits
            ],
            "recent_ta_skips": [
                {"coin": e.get("coin"), "signal": e.get("signal"), "score": e.get("score"), "ts": e.get("ts")}
                for e in recent_ta_skips
            ],
            "recent_research_verdicts": [
                {"coin": e.get("coin"), "verdict": e.get("verdict"),
                 "confidence": e.get("confidence"),
                 "reasoning": (e.get("reasoning") or "")[:160], "ts": e.get("ts")}
                for e in recent_research
            ],
        }
        system_msg = (
            "You are Hermes, the autonomous trading agent's voice. You're embedded in "
            "a Tamagotchi-style dashboard. Be concise (2-4 sentences max), specific, and "
            "operator-grade — no hedging fluff. Answer using ONLY the LIVE STATE below.\n\n"
            "Field map:\n"
            "  • open_positions = live exchange state (the source of truth for what's open)\n"
            "  • recent_trades = last 8 actually-filled trades from memory (with size_usd > 0)\n"
            "  • recent_dsl_exits = positions the DSL exit engine closed (and why)\n"
            "  • recent_research_verdicts = analysis results that fed execution decisions\n"
            "  • recent_ta_skips = signals the TA filter rejected before paid AI research\n\n"
            "Rules: if asked about \"the last trade\", look at recent_trades[-1]. If asked "
            "\"why X\", check recent_research_verdicts for the reasoning. If asked why a "
            "position closed, check recent_dsl_exits. NEVER predict future prices.\n\n"
            f"LIVE STATE: {json.dumps(ctx, default=str)}"
        )
        # Model is env-overridable so the operator can swap without a
        # code change. Default is xAI Grok 4.3 — fast, strong on
        # numeric/financial reasoning, and the operator picked it.
        # Override with HERMES_CHAT_MODEL=<openrouter-slug> in .env.local.
        # Catalog: https://openrouter.ai/models
        chat_model = os.environ.get("HERMES_CHAT_MODEL", "x-ai/grok-4.3")
        # F15: fast-fail while the circuit breaker is open instead of
        # stacking requests onto a dead upstream.
        if _llm_circuit_open():
            return JSONResponse({"response": "Hermes chat temporarily unavailable "
                                             "(upstream circuit open — try again later)",
                                 "kind": "error"})
        try:
            r = await _llm_client().post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": chat_model,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": cmd},
                    ],
                    "max_tokens": 240,
                    "temperature": 0.6,
                },
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            _llm_record_failure()
            # F17: redact before surfacing — an upstream error message can
            # echo the request's Authorization header / API key.
            return JSONResponse({"response": f"chat error: {_redact(e)}",
                                 "kind": "error"})
        _llm_record_success()
        content = data["choices"][0]["message"]["content"].strip()
        return JSONResponse({"response": content, "kind": "chat", "model": chat_model})
    except Exception as e:
        return JSONResponse({"response": f"chat error: {_redact(e)}",
                             "kind": "error"})


# ── route registration ──────────────────────────────────────────────────────


def register_routes(app: FastAPI) -> None:
    """Mount dashboard + SSE + operator routes onto an existing FastAPI app.

    F23: route handlers live in the ``dashboard_routes`` package, split by
    domain — ``public`` (read-only data + SSE + SPA), ``config``
    (operator-gated config management) and ``operator`` (token-gated
    actions). Imports are function-local to avoid a circular import: the
    route modules import shared helpers (``_require_operator``, payload
    builders, terminal handlers) back from this module. Public routes
    (including the SPA history-mode catch-all) register last so their
    ``/{full_path:path}`` route never shadows the config/operator APIs.
    """
    from hermes_trader.dashboard_routes.config import register_config_routes
    from hermes_trader.dashboard_routes.operator import register_operator_routes
    from hermes_trader.dashboard_routes.public import register_public_routes

    register_config_routes(app)
    register_operator_routes(app)
    register_public_routes(app)

