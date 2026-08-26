"""Public + operator web UI for hermes-trader.

Two surfaces, one module:

  GET /                          — public dashboard (anyone)
  GET /operator                  — operator console (token-gated)
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
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from hermes_trader import event_log, session_log
from hermes_trader.agents import dsl_exit
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    backup_config,
    cfg_get,
    create_snapshot,
    list_snapshots,
    read_agent_config,
    restore_backup,
    restore_snapshot,
    write_agent_config,
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
_max_lev_table: Optional[Dict[str, int]] = None


def _load_max_lev_table() -> Dict[str, int]:
    global _max_lev_table
    if _max_lev_table is not None:
        return _max_lev_table
    try:
        from hermes_trader.client.exchange import _get_info
        meta = _get_info().meta() or {}
        _max_lev_table = {
            u["name"]: int(u.get("maxLeverage", 1) or 1)
            for u in meta.get("universe", []) if "name" in u
        }
    except Exception:
        _max_lev_table = {}
    return _max_lev_table


# ── data helpers ─────────────────────────────────────────────────────────────

# Generic TTL cache for read-heavy dashboard endpoints. The dashboard polls
# every few seconds; without this each poll re-reads + re-parses the 800KB+
# session-log JSONL from disk. Keyed by (name, args) so parametrized
# endpoints (equity-curve range, closed-trades limit) cache per-variant.
_TTL_CACHE: Dict[str, tuple] = {}
_TTL_CACHE_LOCK = threading.Lock()


def _ttl_cached(key: str, ttl: float, fn):
    now = time.time()
    with _TTL_CACHE_LOCK:
        hit = _TTL_CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _TTL_CACHE_LOCK:
        _TTL_CACHE[key] = (now, val)
    return val


def _read_log_lines() -> List[Dict[str, Any]]:
    if not _LOG_PATH.exists():
        return []
    out: List[Dict[str, Any]] = []
    with _LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


_EVENTS_PATH = Path(event_log.EVENTS_FILE)


def _read_outcome_lines() -> List[Dict[str, Any]]:
    """Read events.jsonl — the authoritative outcome log that holds reconciled
    `close` events (exchange-triggered / manual-backfill) which may never appear
    in the high-frequency session-log. Each record is nested as
    ``{event, trace_id, timestamp, payload}``."""
    if not _EVENTS_PATH.exists():
        return []
    out: List[Dict[str, Any]] = []
    with _EVENTS_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _iso_to_ms(ts: Any) -> Optional[int]:
    """Parse an ISO-8601 timestamp (as written to events.jsonl) to epoch ms."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        from datetime import datetime, timezone
        s = ts.rstrip("Z")
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
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
    last_event_ts = events[-1]["ts"] if events else 0

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
_POSITIONS_CACHE_TTL_S = 5.0  # acceptable staleness for a display endpoint


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
    except Exception:
        return []
    return _rows_from_state(state)


def _rows_from_state(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Transform a raw HL account state into dashboard position rows, overlaying
    DSL tracker phase/floor from the shared state file. Pure — no network."""
    rows: List[Dict[str, Any]] = []
    for p in state.get("asset_positions", []):
        pos = p.get("position", {})
        coin = pos.get("coin")
        try:
            szi = float(pos.get("szi", "0") or 0)
            entry = float(pos.get("entryPx") or 0)
            mark = float(pos.get("positionValue", 0) or 0) / abs(szi) if szi else 0
            unrealized_usd = float(pos.get("unrealizedPnl", 0) or 0)
            margin_used = float(pos.get("marginUsed", 0) or 0)
        except (TypeError, ValueError):
            continue
        if szi == 0 or not coin:
            continue
        side = "long" if szi > 0 else "short"

        # HL stores leverage as {"value": N, "type": "cross"|"isolated"}; older
        # records (and synthesized stubs) may store it as a bare int.
        leverage_obj = pos.get("leverage")
        if isinstance(leverage_obj, dict):
            leverage = int(leverage_obj.get("value", 1) or 1)
        else:
            leverage = int(leverage_obj or 1)

        spot_pct = ((mark - entry) / entry * 100 if side == "long"
                    else (entry - mark) / entry * 100) if entry else 0
        # ROE = unrealizedPnl / marginUsed — this is what HL's "PNL (ROE %)"
        # column displays, and it already accounts for the open-side fee paid.
        roe_pct = (unrealized_usd / margin_used * 100) if margin_used > 0 else spot_pct * leverage

        tracker = dsl_exit._active_positions.get(f"{coin}_{side}")
        dsl_info = None
        if tracker:
            dsl_info = {
                "peak_px": tracker.peak_px,
                "floor_px": tracker._last_floor,
                "phase": "phase2" if tracker._last_floor and (
                    (side == "long" and tracker._last_floor > tracker.entry_px)
                    or (side == "short" and tracker._last_floor < tracker.entry_px)
                ) else "phase1",
            }

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
    n = len(events)
    cfg_leverage: Optional[int] = None  # lazy-fetched fallback

    def _find_open_side(coin: str, before_idx: int) -> Optional[str]:
        for j in range(before_idx - 1, -1, -1):
            pe = events[j]
            if pe.get("event") == "execute" and pe.get("coin") == coin:
                return pe.get("side")
        return None

    def _cfg_leverage() -> int:
        nonlocal cfg_leverage
        if cfg_leverage is None:
            try:
                cfg_leverage = int(cfg_get("leverage"))
            except Exception:
                cfg_leverage = int(cfg_get("leverage"))
        return cfg_leverage

    def _estimate_leverage(coin: str) -> int:
        # Mirrors executor.py: actual leverage = min(config.leverage, HL per-coin max).
        # Not perfectly accurate for old trades (config may have changed), but
        # closer than config alone — and for most coins HL's cap is the binding one.
        coin_max = _load_max_lev_table().get(coin, 0)
        cfg = _cfg_leverage()
        return min(cfg, coin_max) if coin_max else cfg

    rows: List[Dict[str, Any]] = []

    # ── session-log: dsl_exit / close_position / external_close_recorded / ai_close ──
    for i in range(n - 1, -1, -1):
        e = events[i]
        ev = e.get("event")
        if ev == "dsl_exit":
            coin = e.get("coin", "?")
            side = e.get("side") or _find_open_side(coin, i) or "?"
            has_explicit_lev = e.get("leverage") is not None
            leverage = int(e["leverage"]) if has_explicit_lev else _estimate_leverage(coin)

            # If the close logged an actual fill price, use the realized PnL —
            # it matches HL exactly. Otherwise estimate from the DSL trigger
            # mark and subtract round-trip taker fees.
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

            rows.append({
                "ts": e.get("ts"),
                "coin": coin,
                "source": "dsl",
                "side": side,
                "leverage": leverage,
                "leverage_estimated": not has_explicit_lev,
                "reason": e.get("reason", ""),
                "pnl_pct": net_pnl_pct,
                "pnl_pct_gross": gross_pnl_pct,
                "pnl_source": pnl_source,  # "fill" = exact, "estimated" = pre-trade mid × lev − fees
                "fees_pct": fees_pct,
                "spot_pct": spot_pct,
                "fill_px": e.get("fill_px"),
                "entry_px": e.get("entry_px"),
                "executed": bool(e.get("executed")),
                "detail": e.get("detail"),
            })
        elif ev == "close_position":
            coin = e.get("coin", "?")
            side = e.get("side") or _find_open_side(coin, i) or "?"
            has_explicit_lev = e.get("leverage") is not None
            leverage = int(e["leverage"]) if has_explicit_lev else _estimate_leverage(coin)
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
            rows.append({
                "ts": e.get("ts"),
                "coin": coin,
                "source": "manual",
                "side": side,
                "leverage": leverage,
                "leverage_estimated": not has_explicit_lev,
                "reason": e.get("reason") or "manual_close",
                "pnl_pct": net_pnl_pct,
                "pnl_pct_gross": gross_pnl_pct,
                "pnl_source": pnl_source,
                "fees_pct": fees_pct,
                "spot_pct": spot_pct,
                "fill_px": fill_px,
                "entry_px": entry_px,
                "executed": bool(e.get("ok", e.get("executed"))),
                "detail": e.get("detail"),
            })
        elif ev == "external_close_recorded":
            # Exchange-side close (stop/TP fired off-box). The reconciler also
            # emits a dsl_exit mirror; this branch is kept so older logs that
            # only contain external_close_recorded are still surfaced, and the
            # merge step below de-duplicates against the mirror.
            coin = e.get("coin", "?")
            side = e.get("side") or _find_open_side(coin, i) or "?"
            has_explicit_lev = e.get("leverage") is not None
            leverage = int(e["leverage"]) if has_explicit_lev else _estimate_leverage(coin)
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
            rows.append({
                "ts": e.get("ts"),
                "coin": coin,
                "source": "external",
                "side": side,
                "leverage": leverage,
                "leverage_estimated": not has_explicit_lev,
                "reason": "exchange_trigger",
                "pnl_pct": net_pnl_pct,
                "pnl_pct_gross": gross_pnl_pct,
                "pnl_source": pnl_source,
                "fees_pct": fees_pct,
                "spot_pct": spot_pct,
                "fill_px": exit_px,
                "entry_px": entry_px,
                "executed": True,
                "detail": f"oid={e.get('oid')}" if e.get("oid") else None,
            })
        elif ev == "ai_close":
            # AI verdict close. The event does not carry fill prices/PnL (the
            # actual order result is in close_position_market's return value),
            # so record an audit row with pnl_source="unknown" rather than a
            # misleading 0% that looks like a breakeven trade.
            coin = e.get("coin", "?")
            side = e.get("side") or _find_open_side(coin, i) or "?"
            leverage = _estimate_leverage(coin)
            rows.append({
                "ts": e.get("ts"),
                "coin": coin,
                "source": "ai",
                "side": side,
                "leverage": leverage,
                "leverage_estimated": True,
                "reason": (e.get("reasoning") or "ai_close")[:120],
                "pnl_pct": None,
                "pnl_pct_gross": None,
                "pnl_source": "unknown",
                "fees_pct": None,
                "spot_pct": None,
                "fill_px": None,
                "entry_px": None,
                "executed": bool(e.get("executed")),
                "detail": e.get("detail"),
            })

    # ── events.jsonl: authoritative reconciled `close` records ──
    for rec in _read_outcome_lines():
        if rec.get("event") != "close":
            continue
        p = rec.get("payload") or {}
        coin = p.get("coin", "?")
        ts = p.get("closed_at") or _iso_to_ms(rec.get("timestamp"))
        leverage = int(p.get("leverage") or 0) or _estimate_leverage(coin)
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
        rows.append({
            "ts": ts,
            "coin": coin,
            "source": "reconcile",
            "side": p.get("side") or "?",
            "leverage": leverage,
            "leverage_estimated": p.get("leverage") is None,
            "reason": close_source,
            "pnl_pct": net_pnl_pct,
            "pnl_pct_gross": gross_pnl_pct,
            "pnl_source": pnl_source,
            "fees_pct": fees_pct,
            "spot_pct": spot_pct,
            "fill_px": fill_px,
            "entry_px": entry_px,
            "executed": True,
            "detail": f"hold={p.get('hold_minutes')}m oid={p.get('close_oid')}"
                      if p.get("hold_minutes") is not None else None,
        })

    # ── De-duplicate: same coin within a 5s window describes one fill.
    #    Prefer the richest row (events.jsonl `reconcile`, then `external`,
    #    then `dsl`, then `manual`, then `ai`). ──
    source_rank = {"reconcile": 0, "external": 1, "dsl": 2, "manual": 3, "ai": 4}
    rows.sort(key=lambda r: (
        -(r.get("ts") or 0),
        source_rank.get(r.get("source"), 9),
    ))
    merged: List[Dict[str, Any]] = []
    DEDUP_WINDOW_MS = 5_000
    for r in rows:
        dup = None
        for m in merged:
            if (m.get("coin") == r.get("coin")
                    and m.get("ts") and r.get("ts")
                    and abs(int(m["ts"]) - int(r["ts"])) <= DEDUP_WINDOW_MS):
                dup = m
                break
        if dup is None:
            merged.append(r)
        elif source_rank.get(r.get("source"), 9) < source_rank.get(dup.get("source"), 9):
            merged.remove(dup)
            merged.append(r)

    merged.sort(key=lambda r: -(r.get("ts") or 0))
    return merged[:limit]


def _equity_curve_payload(range_s: int) -> List[Dict[str, Any]]:
    """Series of (ts, equity) points from loop_heartbeat events within `range_s`.

    Filters PARTIAL-DEX degraded reads: a heartbeat that momentarily failed to
    fetch a HIP-3 dex reports equity far below trend (main-dex-only, e.g. $88 vs
    the real $220 aggregate). On a 7d/30d view those show as the account crashing
    to ~$20 and back, and they crush the y-axis. Capped positions can't lose tens
    of % in one 60s tick, so a point far below the TRAILING median of accepted
    points is a bad read, not a real move — and using the *trailing* (not global)
    median preserves genuine gradual growth across the window.
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
        if window and eq < 0.7 * ref:
            continue  # partial-dex degraded read — drop it
        series.append({"ts": ts, "equity": round(eq, 2)})
        window.append(eq)
        if len(window) > 15:
            window.pop(0)
    return series


# ── SSE feed ─────────────────────────────────────────────────────────────────


async def _tail_log_sse() -> AsyncIterator[str]:
    """Stream new session-log lines as SSE events. Replays the last 500 first."""
    # Replay buffer so a fresh connection sees the recent past, not just future events.
    # session_log.tail() reads backward from end of file in chunks; offload to a
    # thread so it doesn't block the event loop for other routes / SSE clients.
    replay = await asyncio.to_thread(session_log.tail, 500)
    for e in replay:
        yield f"data: {json.dumps(e)}\n\n"

    def _stat_size() -> int:
        return _LOG_PATH.stat().st_size if _LOG_PATH.exists() else 0

    last_size = await asyncio.to_thread(_stat_size)
    # Heartbeat every 15s keeps proxies (nginx, Cloudflare) from closing idle SSE.
    last_heartbeat = time.time()

    def _read_new_lines(prev_size: int) -> tuple[list[str], int]:
        """Read & validate new lines appended since prev_size. Runs in a thread
        because open()/read() on a multi-MB log would block the event loop."""
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

    while True:
        await asyncio.sleep(1.0)
        new_lines, new_size = await asyncio.to_thread(_read_new_lines, last_size)
        if new_size < last_size:
            last_size = 0  # rotated
        if new_lines:
            for line in new_lines:
                yield f"data: {line}\n\n"
            last_size = new_size
        elif new_size != last_size:
            last_size = new_size

        if time.time() - last_heartbeat > 15:
            yield ": keepalive\n\n"
            last_heartbeat = time.time()


# ── operator gate ────────────────────────────────────────────────────────────


def _extract_bearer(request: Request) -> str:
    """Return the bearer token from ``Authorization: Bearer <token>`` or ``''``."""
    auth = request.headers.get("Authorization") or request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return ""


def _require_operator(request: Request) -> None:
    """401 unless a valid operator token is supplied.

    Accepted token transports (in priority order):
      1. Standard ``Authorization: Bearer <token>`` header (preferred)
      2. Legacy ``X-Operator-Token`` header
      3. ``?token=<token>`` query parameter (deprecated, kept for browser panels)

    Checking at request time (not import time) means rotating the token doesn't
    need a restart. Missing env var = operator surface is closed.
    """
    expected = os.environ.get("HERMES_OPERATOR_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="operator surface disabled (set HERMES_OPERATOR_TOKEN)")
    provided = (
        _extract_bearer(request)
        or request.headers.get("X-Operator-Token", "")
        or request.query_params.get("token", "")
    )
    if provided != expected:
        # Send a WWW-Authenticate challenge so standard HTTP clients know to
        # present a bearer token (P0-2).
        raise HTTPException(
            status_code=401,
            detail="invalid operator token",
            headers={"WWW-Authenticate": 'Bearer realm="hermes-trader"'},
        )


# ── HTML ─────────────────────────────────────────────────────────────────────


_PUBLIC_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>hermes-trader · live</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap" rel="stylesheet">
<!-- NES.css — pixel-perfect Nintendo-flavored UI primitives. Spike: wraps the
     hamster habitat as a proper Tamagotchi enclosure. Tiny (~30KB), no JS. -->
<link rel="stylesheet" href="https://unpkg.com/nes.css@2.3.0/css/nes.min.css">
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  body{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#0a0a0a;color:#e5e5e5;image-rendering:pixelated}
  /* Discreet mode — show `•••` instead of every dollar value. The mask span
     sits next to the value span; CSS flips which is visible. */
  .dollar-mask{display:none}
  body.discreet .dollar-value{display:none}
  body.discreet .dollar-mask{display:inline;color:#71717a}
  /* Pixel-font headings only — body text stays readable mono. */
  .pixel{font-family:'Press Start 2P',ui-monospace,monospace;letter-spacing:.02em;line-height:1.4}
  /* Primary navbar links — NES-button-flavored with a clear active state. */
  .nav-link{display:inline-block;padding:7px 11px;font-size:9px;letter-spacing:.12em;color:#a3a3a3;background:#18181b;border:2px solid #3f3f46;box-shadow:2px 2px 0 #0a0a0a;text-decoration:none;transition:transform .08s ease,box-shadow .08s ease}
  .nav-link:hover{color:#a7f3d0;border-color:#047857;box-shadow:2px 2px 0 #022c1e}
  .nav-link:active{transform:translate(2px,2px);box-shadow:none}
  .nav-link.nav-active{background:#064e3b;color:#6ee7b7;border-color:#34d399;box-shadow:2px 2px 0 #022c1e}
  .nav-link.nav-link-ghost{background:transparent;border-color:#27272a;color:#71717a}
  .nav-link.nav-link-ghost:hover{color:#fde047;border-color:#78350f}
  /* Chunky pixel-card: 2px border + hard 4px offset shadow, no rounded corners. */
  .pixel-card{border:2px solid #27272a;box-shadow:4px 4px 0 #18181b;background:#0f0f10;border-radius:0}
  .pixel-card.accent{border-color:#34d399;box-shadow:4px 4px 0 #064e3b}
  .pixel-btn{border:2px solid currentColor;box-shadow:2px 2px 0 #18181b;border-radius:0;image-rendering:pixelated}
  .pixel-btn:active{transform:translate(2px,2px);box-shadow:none}
  /* LCD-style title strip */
  .lcd{background:#052e1c;border:2px solid #34d399;box-shadow:inset 0 0 0 1px #022c1e,4px 4px 0 #064e3b;padding:8px 12px;color:#6ee7b7;text-shadow:0 0 6px #34d39966}
  /* Agent pet — bounces gently */
  .pet{font-size:28px;display:inline-block;animation:pet-bounce 1.4s ease-in-out infinite;filter:drop-shadow(2px 2px 0 #064e3b)}
  /* When the pet element renders the pixel-sprite SVG (worried state), size
     it like the emoji glyphs so the bounce animation lines up. */
  .pet.pet-sprite{width:32px;height:32px;font-size:0;line-height:0;image-rendering:pixelated}
  .pet.pet-sprite svg{width:100%;height:100%;display:block;image-rendering:pixelated}
  @keyframes pet-bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-4px)}}
  .pet.shake{animation:pet-shake 0.4s linear infinite}
  @keyframes pet-shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-2px)}75%{transform:translateX(2px)}}
  .pet.sleep{animation:pet-sleep 3s ease-in-out infinite;filter:none;opacity:.7}
  @keyframes pet-sleep{0%,100%{transform:scale(1)}50%{transform:scale(0.95)}}
  /* Pixel "mood bar" — chunky blocks */
  .mood-bar{font-family:'Press Start 2P',monospace;font-size:10px;letter-spacing:2px;color:#34d399}
  .feed-row{font-size:12px;line-height:1.6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .feed-row.scan{color:#9ca3af}
  .feed-row.research{color:#a5b4fc}
  .feed-row.execute{color:#86efac}
  .feed-row.execute-fail{color:#fca5a5}
  .feed-row.error{color:#f87171}
  .feed-row.heartbeat{color:#71717a}
  .feed-row.dsl_exit{color:#fbbf24}
  /* Status pill — pixel-block style with sharp corners */
  .pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;font-size:10px;font-weight:600;font-family:'Press Start 2P',monospace;border:2px solid currentColor;border-radius:0;letter-spacing:.05em}
  .pill.scanning{background:#064e3b;color:#6ee7b7}
  .pill.stale{background:#451a03;color:#fbbf24}
  .pill.offline{background:#450a0a;color:#fca5a5}
  .num{font-variant-numeric:tabular-nums}
  .blink{animation:blink 1.6s ease-in-out infinite}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:.4}}
  /* Override Tailwind's rounded-lg on existing sections to keep pixel feel */
  section.bg-zinc-900{border:2px solid #27272a;box-shadow:4px 4px 0 #18181b;border-radius:0;background:#0f0f10}
  /* ── Matrix-rain right sidebar ── */
  .matrix-pane{
    background:linear-gradient(180deg,#02110a 0%,#000805 100%);
    border:2px solid #047857;
    box-shadow:4px 4px 0 #022c1e, inset 0 0 24px rgba(52,211,153,0.08);
    border-radius:0;
    position:relative;overflow:hidden;
  }
  .matrix-pane::before{
    /* scanline overlay — barely visible, sells the CRT vibe */
    content:'';position:absolute;inset:0;pointer-events:none;
    background:repeating-linear-gradient(0deg,rgba(0,0,0,0) 0,rgba(0,0,0,0) 2px,rgba(0,0,0,0.18) 3px,rgba(0,0,0,0) 4px);
    z-index:1;
  }
  .matrix-feed{position:relative;z-index:2;overflow-y:auto;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .matrix-feed .feed-row{
    /* Override the global .feed-row nowrap/ellipsis — in the matrix sidebar we
       want full readability over single-line truncation. Hanging indent so the
       wrapped continuation lines sit under the message body, not under the
       timestamp. */
    white-space:normal;word-break:break-word;overflow:visible;text-overflow:clip;
    text-indent:-1.25em;padding:2px 2px 2px 1.5em;line-height:1.5;
    color:#6ee7b7;text-shadow:0 0 4px rgba(52,211,153,0.5);
    animation:matrix-in .55s cubic-bezier(.2,.7,.3,1);
  }
  /* Last row glows a bit hotter — like the "head" of a matrix stream */
  .matrix-feed .feed-row:last-child{color:#a7f3d0;text-shadow:0 0 8px rgba(167,243,208,0.7)}
  .matrix-feed .feed-row.error{color:#fca5a5;text-shadow:0 0 4px rgba(248,113,113,0.5)}
  .matrix-feed .feed-row.execute{color:#86efac;text-shadow:0 0 6px rgba(134,239,172,0.6)}
  .matrix-feed .feed-row.dsl_exit{color:#fde68a;text-shadow:0 0 6px rgba(253,230,138,0.6)}
  .matrix-feed::-webkit-scrollbar{width:6px}
  .matrix-feed::-webkit-scrollbar-track{background:#02110a}
  .matrix-feed::-webkit-scrollbar-thumb{background:#047857}
  @keyframes matrix-in{
    0%{opacity:0;transform:translateY(-6px);filter:blur(1px)}
    60%{opacity:.85}
    100%{opacity:1;transform:translateY(0);filter:blur(0)}
  }
  /* Sticky on wide screens, scrolls inline on narrow */
  @media (min-width:1024px){.matrix-pane{position:sticky;top:1.5rem;height:calc(100vh - 3rem)}}
  /* ── HERMES.HAMSTER habitat — Tamagotchi pet enclosure ── */
  /* NES.css supplies the chunky pixel border/dark fill; we only override
     spacing + position so it sits cleanly at the top of the matrix sidebar. */
  .habitat-nes{margin:8px 8px 6px;padding:12px !important;background:#020a05 !important;border-color:#047857 !important;position:relative;z-index:3}
  /* Layout: rabbit + spinning wheel on the left, NES speech balloon on the right. */
  .habitat-pet-section{display:flex;align-items:flex-start;gap:8px}
  .habitat-balloon{flex:1;margin:0 !important;padding:6px 8px !important;min-width:0}
  .habitat-balloon::after,.habitat-balloon::before{filter:hue-rotate(0)}
  #hamster-quote{font-family:'Press Start 2P',monospace;font-size:7px;color:#6ee7b7;text-align:left;line-height:1.6;margin:0;letter-spacing:.04em;transition:opacity .3s ease;word-break:break-word}
  .habitat-name{font-family:'Press Start 2P',monospace;font-size:8px;color:#34d399;letter-spacing:.15em;text-align:center;margin-bottom:4px;text-shadow:0 0 4px rgba(52,211,153,0.6)}
  .habitat-name .psi{color:#fbbf24;margin-left:4px;text-shadow:0 0 6px rgba(251,191,36,0.6)}
  .habitat-pet{display:flex;flex-direction:column;align-items:center;gap:0;padding:2px 0 4px;line-height:1}
  /* Pixel-art container — sized for either the inline SVG rabbit sprite or
     a fallback emoji (when a PnL state swaps the contents). image-rendering
     pixelated keeps the SVG crisp even when scaled. */
  .hamster-body{font-size:30px;display:inline-block;width:36px;height:36px;line-height:36px;text-align:center;animation:hamster-run .42s ease-in-out infinite;filter:drop-shadow(0 0 6px rgba(255,255,255,0.65));image-rendering:pixelated}
  .hamster-body svg{width:100%;height:100%;display:block;image-rendering:pixelated}
  @keyframes hamster-run{0%,100%{transform:translateY(0)}50%{transform:translateY(-3px) rotate(-2deg)}}
  .hamster-wheel{font-size:14px;display:inline-block;animation:wheel-spin .8s linear infinite;opacity:.75;color:#34d399}
  @keyframes wheel-spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}
  /* (legacy .habitat-quote removed in favor of NES.css balloon — see #hamster-quote above) */
  /* ── Hermes terminal modal — Cmd+K opens a NES-styled command line ── */
  .hermes-modal{position:fixed;inset:0;z-index:80;display:flex;align-items:flex-start;justify-content:center;padding-top:8vh;pointer-events:auto}
  .hermes-modal.hidden{display:none}
  .hermes-modal-bg{position:absolute;inset:0;background:rgba(0,0,0,0.75);backdrop-filter:blur(2px)}
  .hermes-modal-box{position:relative;z-index:1;width:min(680px,92vw);max-height:80vh;display:flex;flex-direction:column;padding:14px !important;background:#020a05 !important;border-color:#34d399 !important;box-shadow:6px 6px 0 #064e3b !important}
  .hermes-modal-header{display:flex;align-items:center;justify-content:space-between;border-bottom:2px solid #047857;padding-bottom:6px;margin-bottom:8px}
  .hermes-modal-title{font-size:10px;letter-spacing:.15em;color:#34d399;text-shadow:0 0 6px rgba(52,211,153,0.6)}
  .hermes-modal-close{background:transparent;border:0;color:#34d399;font-family:'Press Start 2P',monospace;font-size:14px;cursor:pointer;padding:0 4px;line-height:1}
  .hermes-modal-close:hover{color:#a7f3d0}
  .hermes-history{flex:1;overflow-y:auto;font-family:ui-monospace,monospace;font-size:12px;line-height:1.55;color:#6ee7b7;padding:4px 2px;min-height:200px;max-height:50vh}
  .hermes-line{margin-bottom:4px;white-space:pre-wrap;word-break:break-word}
  .hermes-line.hermes-meta{color:#52525b;font-size:10px}
  .hermes-line.hermes-cmd{color:#a7f3d0;text-shadow:0 0 4px rgba(167,243,208,0.6)}
  .hermes-line.hermes-action{color:#fde047}
  .hermes-line.hermes-error{color:#fca5a5}
  .hermes-line.hermes-chat{color:#a5b4fc}
  .hermes-line.hermes-status{color:#86efac}
  .hermes-input-row{display:flex;align-items:center;gap:6px;border-top:2px solid #047857;padding-top:8px;margin-top:4px}
  .hermes-prompt{font-family:ui-monospace,monospace;color:#34d399;font-weight:700;text-shadow:0 0 4px rgba(52,211,153,0.7)}
  .hermes-input{flex:1;background:#000;border:1px solid #064e3b;color:#a7f3d0;font-family:ui-monospace,monospace;font-size:13px;padding:6px 8px;outline:0}
  .hermes-input:focus{border-color:#34d399;box-shadow:0 0 6px rgba(52,211,153,0.5)}
  /* ── Hamster reactions to live trading events ── */
  /* execute → yellow celebrate (lightning bolt burst) */
  .hamster-body.celebrate{animation:hamster-celebrate 1.2s ease-out}
  @keyframes hamster-celebrate{
    0%{transform:translateY(0) scale(1) rotate(0);filter:drop-shadow(0 0 4px rgba(251,191,36,0.5))}
    20%{transform:translateY(-10px) scale(1.35) rotate(-15deg);filter:drop-shadow(0 0 14px #fde047) brightness(1.4)}
    40%{transform:translateY(-6px) scale(1.25) rotate(15deg)}
    60%{transform:translateY(-3px) scale(1.15) rotate(-8deg)}
    100%{transform:translateY(0) scale(1) rotate(0)}
  }
  /* dsl_exit profitable → victory wiggle */
  .hamster-body.victory{animation:hamster-victory 1.4s ease-out}
  @keyframes hamster-victory{
    0%,100%{transform:rotate(0) scale(1)}
    15%{transform:rotate(-20deg) scale(1.2);filter:drop-shadow(0 0 10px #34d399)}
    30%{transform:rotate(20deg) scale(1.2)}
    45%{transform:rotate(-15deg) scale(1.15)}
    60%{transform:rotate(15deg) scale(1.1)}
    75%{transform:rotate(-5deg)}
  }
  /* dsl_exit loss → defeat shake */
  .hamster-body.defeat{animation:hamster-defeat 1.2s ease-out}
  @keyframes hamster-defeat{
    0%,100%{transform:translateX(0) rotate(0)}
    10%,30%,50%,70%,90%{transform:translateX(-4px) rotate(-6deg);filter:drop-shadow(0 0 8px #f87171)}
    20%,40%,60%,80%{transform:translateX(4px) rotate(6deg)}
  }
  /* Habitat background flash on event */
  .habitat.flash-yellow{animation:habitat-flash-y 1.2s ease-out}
  @keyframes habitat-flash-y{0%{background:linear-gradient(180deg,#3f2e00 0%,#150e00 100%);box-shadow:inset 0 0 20px rgba(251,191,36,0.4)}100%{background:linear-gradient(180deg,#02160c 0%,#000805 100%)}}
  .habitat.flash-green{animation:habitat-flash-g 1.2s ease-out}
  @keyframes habitat-flash-g{0%{background:linear-gradient(180deg,#064e3b 0%,#001f12 100%);box-shadow:inset 0 0 20px rgba(52,211,153,0.4)}100%{background:linear-gradient(180deg,#02160c 0%,#000805 100%)}}
  .habitat.flash-red{animation:habitat-flash-r 1.2s ease-out}
  @keyframes habitat-flash-r{0%{background:linear-gradient(180deg,#450a0a 0%,#1f0000 100%);box-shadow:inset 0 0 20px rgba(248,113,113,0.4)}100%{background:linear-gradient(180deg,#02160c 0%,#000805 100%)}}
  /* Floating burst icon — ⚡/💰/💀 rises and fades */
  .habitat-burst{position:absolute;top:32px;left:50%;font-size:20px;opacity:0;pointer-events:none;z-index:5;text-shadow:0 0 8px rgba(255,255,255,0.6)}
  .habitat-burst.show{animation:burst-rise 1.1s ease-out forwards}
  @keyframes burst-rise{
    0%{opacity:0;transform:translateX(-50%) translateY(0) scale(0.5)}
    20%{opacity:1;transform:translateX(-50%) translateY(-8px) scale(1.5)}
    100%{opacity:0;transform:translateX(-50%) translateY(-36px) scale(1)}
  }
</style>
</head>
<body class="min-h-screen">
<div class="max-w-[1600px] mx-auto px-6 py-6">

  <header class="flex items-center justify-between mb-3 gap-3 flex-wrap">
    <div class="flex items-center gap-3">
      <span id="pet" class="pet" title="agent mood — reacts to status + PnL">🤖</span>
      <div class="flex flex-col">
        <span class="lcd pixel text-sm tracking-tight">HERMES-TRADER</span>
        <span class="text-[10px] text-zinc-500 mt-1 pixel">AUTONOMOUS · HYPERLIQUID</span>
      </div>
    </div>
    <div class="flex items-center gap-2 text-xs">
      <select id="ccy-sel" class="bg-zinc-800 text-zinc-300 rounded px-2 py-1 text-xs border-0 focus:outline-none cursor-pointer">
        <option value="USD">USD $</option>
        <option value="EUR">EUR €</option>
        <option value="JPY">JPY ¥</option>
        <option value="GBP">GBP £</option>
        <option value="CNY">CNY ¥</option>
        <option value="KRW">KRW ₩</option>
        <option value="SGD">SGD S$</option>
        <option value="PHP">PHP ₱</option>
        <option value="MYR">MYR RM</option>
        <option value="THB">THB ฿</option>
        <option value="IDR">IDR Rp</option>
        <option value="VND">VND ₫</option>
        <option value="AUD">AUD A$</option>
        <option value="CAD">CAD C$</option>
        <option value="CHF">CHF</option>
      </select>
      <select id="lang-sel" class="bg-zinc-800 text-zinc-300 rounded px-2 py-1 text-xs border-0 focus:outline-none cursor-pointer">
        <option value="en">EN</option>
        <option value="zh">中文</option>
        <option value="ja">日本語</option>
        <option value="ko">한국어</option>
        <option value="fr">Français</option>
        <option value="es">Español</option>
        <option value="id">Bahasa</option>
        <option value="tl">Tagalog</option>
        <option value="vi">Tiếng Việt</option>
        <option value="th">ไทย</option>
      </select>
      <!-- Discreet-mode toggle: hides $ amounts (equity, PnL, position size,
           chart axis/tooltip) while leaving every % visible. State persists
           in localStorage. Click to flip 👁 ↔ 🙈. -->
      <button id="discreet-toggle" type="button" class="bg-zinc-800 text-zinc-300 rounded px-2 py-1 text-xs border-0 cursor-pointer hover:bg-zinc-700" title="hide all $ amounts (keep percentages)">👁</button>
      <!-- Operator-mode toggle: prompts for HERMES_OPERATOR_TOKEN, stashes it
           in localStorage, reloads with ?token= so the Hermes terminal
           (Cmd+K) + operator endpoints unlock. 🔒 = read-only, 🔓 = operator. -->
      <button id="operator-toggle" type="button" class="bg-zinc-800 text-zinc-300 rounded px-2 py-1 text-xs border-0 cursor-pointer hover:bg-zinc-700" title="enter operator mode — unlocks Hermes terminal (Cmd+K)">🔒 op</button>
      <span id="status-pill" class="pill offline">offline</span>
      <a href="https://github.com/Julian-dev28/hermes-trader" class="text-zinc-400 hover:text-zinc-200">github</a>
    </div>
  </header>

  <!-- ── primary navbar: clear page links, NES-button styled, current page
       highlighted via the .nav-active class set in JS at the bottom. ── -->
  <nav class="flex items-center gap-2 mb-6 flex-wrap" id="hermes-nav">
    <a href="/" data-nav="/" class="nav-link pixel">DASHBOARD</a>
    <a href="/config" data-nav="/config" class="nav-link pixel">CONFIG</a>
    <a href="/operator" data-nav="/operator" class="nav-link pixel">OPERATOR</a>
    <span class="text-zinc-700 mx-1">·</span>
    <a href="javascript:void(0)" onclick="document.dispatchEvent(new KeyboardEvent('keydown',{key:'k',metaKey:true}))" class="nav-link pixel nav-link-ghost">⌘K TERMINAL</a>
  </nav>

  <div class="grid grid-cols-1 lg:grid-cols-[1fr_560px] gap-6">
  <main class="min-w-0 space-y-6">

  <section class="grid grid-cols-2 md:grid-cols-4 gap-4">
    <div class="bg-zinc-900 rounded-lg p-4">
      <div class="text-[10px] text-zinc-500 pixel" data-i18n="equity">equity</div>
      <div class="text-2xl font-bold num" id="kpi-equity">$0.00</div>
      <div class="text-[10px] text-zinc-500 num mt-1" id="kpi-equity-breakdown" title="free margin available across all dexes"></div>
    </div>
    <div class="bg-zinc-900 rounded-lg p-4">
      <div class="text-[10px] text-zinc-500 pixel" data-i18n="today">today</div>
      <div class="text-2xl font-bold num" id="kpi-pnl">$0.00</div>
      <div class="text-xs num" id="kpi-pnl-pct">—</div>
    </div>
    <div class="bg-zinc-900 rounded-lg p-4">
      <div class="text-[10px] text-zinc-500 pixel" data-i18n="open_positions">open positions</div>
      <div class="text-2xl font-bold num" id="kpi-open">0</div>
    </div>
    <div class="bg-zinc-900 rounded-lg p-4">
      <div class="text-[10px] text-zinc-500 pixel" data-i18n="last_tick">last tick</div>
      <div class="text-2xl font-bold num" id="kpi-tick">—</div>
      <div class="text-[10px] text-zinc-500 pixel" id="kpi-tick-detail" data-i18n="no_scan_yet">no scan yet</div>
    </div>
  </section>

  <section class="bg-zinc-900 rounded-lg p-4 text-xs leading-relaxed text-zinc-400">
    <div class="text-zinc-500 mb-2 uppercase tracking-wider text-[10px]">how it works</div>
    <p>
      Autonomous trading agent on
      <a class="text-emerald-400 hover:underline" href="https://hyperliquid.xyz">Hyperliquid</a>
      perpetuals — crypto, equities, commodities.
      Every minute the engine scans 60+ markets for statistical triggers (volume
      spikes, breakouts, momentum bursts), runs a free TA filter, and only spends
      AI tokens on confirmed setups. Trades clear 11 risk gates, size by half-Kelly,
      and exit through a two-phase dynamic stop-loss (loss protection → profit
      locking with one-way trailing floor).
      <span class="text-zinc-500">Live on one wallet. Not financial advice.</span>
    </p>
  </section>

  <section class="bg-zinc-900 rounded-lg p-4">
    <div class="flex items-center justify-between mb-2">
      <div class="text-[10px] text-zinc-500 pixel" data-i18n="equity_curve">equity curve</div>
      <div class="flex gap-1 text-xs">
        <button data-range="86400" class="range-btn px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700">24h</button>
        <button data-range="604800" class="range-btn px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700">7d</button>
        <button data-range="2592000" class="range-btn px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700">30d</button>
      </div>
    </div>
    <div class="relative">
      <canvas id="equity-chart" height="110"></canvas>
      <div id="equity-empty" class="hidden absolute inset-0 flex items-center justify-center text-xs text-zinc-500">
        no heartbeats yet in this window
      </div>
    </div>
  </section>

  <section class="bg-zinc-900 rounded-lg p-4">
    <div class="flex items-center justify-between mb-2">
      <div class="text-[10px] text-zinc-500 pixel" data-i18n="open_positions">open positions</div>
      <div class="flex gap-1 text-[10px]">
        <button data-sort="default" data-i18n="default" class="pos-sort-btn px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700">default</button>
        <button data-sort="pnl_desc" class="pos-sort-btn px-2 py-0.5 rounded bg-emerald-700">PnL ↓</button>
        <button data-sort="pnl_asc"  class="pos-sort-btn px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700">PnL ↑</button>
      </div>
    </div>
    <div id="positions" class="text-sm">
      <div class="text-zinc-500 text-xs" data-i18n="none">none</div>
    </div>
  </section>

  <section class="bg-zinc-900 rounded-lg p-4">
    <div class="flex items-center justify-between mb-2">
      <div class="text-[10px] text-zinc-500 pixel" data-i18n="recent_closes">recent closes</div>
      <div class="text-xs text-zinc-600" id="closes-stats"></div>
    </div>
    <div id="closes" class="text-sm">
      <div class="text-zinc-500 text-xs" data-i18n="none_yet">none yet</div>
    </div>
  </section>

  </main>

  <aside class="matrix-pane flex flex-col">
    <div class="nes-container is-dark is-rounded habitat habitat-nes">
      <section class="habitat-pet-section">
        <div class="habitat-pet">
          <span class="hamster-body" title="follow the white cat">
            <!-- 16x16 pixel cat sprite — hand-rolled NES-style. Heterochromia:
                 left eye blue, right eye green. shape-rendering=crispEdges
                 keeps the pixels sharp at any scale. -->
            <svg viewBox="0 0 16 16" shape-rendering="crispEdges" xmlns="http://www.w3.org/2000/svg">
              <!-- triangular ears -->
              <rect x="4" y="1" width="1" height="1" fill="#f5f5f5"/>
              <rect x="3" y="2" width="3" height="1" fill="#f5f5f5"/>
              <rect x="2" y="3" width="4" height="1" fill="#f5f5f5"/>
              <rect x="11" y="1" width="1" height="1" fill="#f5f5f5"/>
              <rect x="10" y="2" width="3" height="1" fill="#f5f5f5"/>
              <rect x="10" y="3" width="4" height="1" fill="#f5f5f5"/>
              <!-- inner pink ear -->
              <rect x="4" y="3" width="1" height="1" fill="#fbcfe8"/>
              <rect x="11" y="3" width="1" height="1" fill="#fbcfe8"/>
              <!-- head + body -->
              <rect x="2" y="4" width="12" height="9" fill="#f5f5f5"/>
              <!-- heterochromia eyes: green left, blue right -->
              <rect x="4" y="7" width="2" height="2" fill="#15803d"/>
              <rect x="10" y="7" width="2" height="2" fill="#3b82f6"/>
              <!-- pink nose -->
              <rect x="7" y="10" width="2" height="1" fill="#f472b6"/>
              <!-- whiskers -->
              <rect x="0" y="10" width="2" height="1" fill="#a3a3a3"/>
              <rect x="14" y="10" width="2" height="1" fill="#a3a3a3"/>
              <!-- paws -->
              <rect x="3" y="13" width="2" height="1" fill="#d4d4d4"/>
              <rect x="11" y="13" width="2" height="1" fill="#d4d4d4"/>
            </svg>
          </span>
          <span class="hamster-wheel">⚙</span>
        </div>
        <div class="nes-balloon from-left is-dark habitat-balloon">
          <p id="hamster-quote">awakening</p>
        </div>
      </section>
    </div>
    <div class="flex items-center justify-between px-3 py-2 border-b-2 border-emerald-800/60 bg-black/40 relative z-10">
      <div class="text-[10px] text-emerald-400 pixel" data-i18n="live_activity">live activity</div>
      <span class="text-[10px] text-emerald-400 blink pixel" data-i18n="following">▶ following</span>
    </div>
    <div id="feed" class="matrix-feed flex-1 px-3 py-2 space-y-0.5"></div>
  </aside>
  </div>

  <footer class="text-[10px] text-zinc-600 mt-6 text-center pixel" data-i18n="footer">
    one wallet · live · not financial advice
  </footer>
</div>

<!-- ── Hermes terminal modal — Cmd+K (or Ctrl+K) toggle. Operator-token gated
     via the same ?token= the page was loaded with. Built-in commands resolve
     locally; free text falls through to the chat model (default xAI Grok 4.3,
     override via HERMES_CHAT_MODEL env var) over OpenRouter. ── -->
<div id="hermes-modal" class="hermes-modal hidden">
  <div class="hermes-modal-bg"></div>
  <div class="nes-container is-dark is-rounded hermes-modal-box">
    <div class="hermes-modal-header">
      <span class="pixel hermes-modal-title">HERMES // TERMINAL</span>
      <button id="hermes-close" class="pixel hermes-modal-close" title="close (esc)">×</button>
    </div>
    <div id="hermes-history" class="hermes-history">
      <div class="hermes-line hermes-meta">type `help` for commands · esc to close · free text → Grok 4.3</div>
    </div>
    <div class="hermes-input-row">
      <span class="hermes-prompt">▸</span>
      <input id="hermes-input" type="text" autocomplete="off" spellcheck="false" class="hermes-input" placeholder="status, pause, close BTC, or ask Hermes anything…" />
    </div>
  </div>
</div>

<script>
// ── locale / currency state ──
// USD values from the API are multiplied by ccyState.rate at display time. FX
// rates pulled from open.er-api.com (free, no key) and cached 1h in localStorage.
let ccyState = { code: 'USD', rate: 1 };
let langState = 'en';
const ZERO_DECIMAL_CCY = new Set(['JPY', 'KRW', 'VND', 'IDR']);

function fmtMoney(usd, opts = {}) {
  const v = (usd ?? 0) * ccyState.rate;
  const digits = ZERO_DECIMAL_CCY.has(ccyState.code) ? 0 : 2;
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: ccyState.code,
      signDisplay: opts.signed ? 'always' : 'auto',
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(v);
  } catch (e) {
    return (v >= 0 ? '' : '-') + Math.abs(v).toFixed(digits);
  }
}
const fmtPct = n => (n >= 0 ? '+' : '') + n.toFixed(2) + '%';

// ── discreet mode ──────────────────────────────────────────────────────────
// When `body.discreet`, all $ amounts render as `•••` (KPIs, position PnL $,
// chart y-axis, chart tooltip, feed heartbeat $). Percentages stay visible.
function isDiscreet() { return document.body.classList.contains('discreet'); }
function maskDollar(text) { return isDiscreet() ? '•••' : text; }
// HTML helper for elements whose textContent is set imperatively (KPIs):
// wraps the value in matched .dollar-value / .dollar-mask spans.
function dollarHTML(value) {
  return `<span class="dollar-value">${value}</span><span class="dollar-mask">•••</span>`;
}
const fmtAge = s => s == null ? '—' : (s < 60 ? s + 's' : Math.floor(s/60) + 'm ago');
const fmtTime = ms => { const d = new Date(ms); return d.toTimeString().slice(0,8); };

// ── i18n ──
const I18N = {
  en: { equity:'equity', today:'today', open_positions:'open positions', last_tick:'last tick', no_scan_yet:'no scan yet', equity_curve:'equity curve', recent_closes:'recent closes', live_activity:'live activity', none:'none', none_yet:'none yet', following:'▶ following', footer:'one wallet · live · not financial advice', default:'default', triggers:'triggers' },
  zh: { equity:'净值', today:'今日', open_positions:'持仓', last_tick:'上次更新', no_scan_yet:'尚未扫描', equity_curve:'净值曲线', recent_closes:'最近平仓', live_activity:'实时活动', none:'无', none_yet:'尚无', following:'▶ 关注中', footer:'单一钱包 · 实盘 · 非投资建议', default:'默认', triggers:'触发' },
  ja: { equity:'純資産', today:'本日', open_positions:'ポジション', last_tick:'最終更新', no_scan_yet:'スキャン未実施', equity_curve:'純資産推移', recent_closes:'最近のクローズ', live_activity:'ライブアクティビティ', none:'なし', none_yet:'まだなし', following:'▶ 追跡中', footer:'単一ウォレット · ライブ · 投資助言ではありません', default:'デフォルト', triggers:'トリガー' },
  ko: { equity:'자본', today:'오늘', open_positions:'보유 포지션', last_tick:'마지막 틱', no_scan_yet:'스캔 전', equity_curve:'자본 곡선', recent_closes:'최근 청산', live_activity:'실시간 활동', none:'없음', none_yet:'아직 없음', following:'▶ 추적 중', footer:'단일 지갑 · 실시간 · 투자 조언 아님', default:'기본', triggers:'트리거' },
  fr: { equity:'capital', today:"aujourd'hui", open_positions:'positions ouvertes', last_tick:'dernier tick', no_scan_yet:'pas encore scanné', equity_curve:'courbe du capital', recent_closes:'clôtures récentes', live_activity:'activité en direct', none:'aucune', none_yet:'aucune encore', following:'▶ en cours', footer:'un portefeuille · en direct · pas un conseil financier', default:'défaut', triggers:'déclencheurs' },
  es: { equity:'capital', today:'hoy', open_positions:'posiciones abiertas', last_tick:'último tick', no_scan_yet:'sin escaneo aún', equity_curve:'curva de capital', recent_closes:'cierres recientes', live_activity:'actividad en vivo', none:'ninguna', none_yet:'ninguna aún', following:'▶ siguiendo', footer:'una cartera · en vivo · no es consejo financiero', default:'por defecto', triggers:'disparadores' },
  id: { equity:'ekuitas', today:'hari ini', open_positions:'posisi terbuka', last_tick:'tick terakhir', no_scan_yet:'belum pindai', equity_curve:'kurva ekuitas', recent_closes:'penutupan terbaru', live_activity:'aktivitas langsung', none:'tidak ada', none_yet:'belum ada', following:'▶ mengikuti', footer:'satu dompet · langsung · bukan nasihat keuangan', default:'bawaan', triggers:'pemicu' },
  tl: { equity:'puhunan', today:'ngayon', open_positions:'bukas na posisyon', last_tick:'huling tick', no_scan_yet:'wala pang scan', equity_curve:'kurba ng puhunan', recent_closes:'kamakailang isinara', live_activity:'live na aktibidad', none:'wala', none_yet:'wala pa', following:'▶ sumusunod', footer:'isang wallet · live · hindi payong pinansyal', default:'default', triggers:'trigger' },
  vi: { equity:'vốn', today:'hôm nay', open_positions:'vị thế mở', last_tick:'tick cuối', no_scan_yet:'chưa quét', equity_curve:'đường vốn', recent_closes:'đóng gần đây', live_activity:'hoạt động trực tiếp', none:'không có', none_yet:'chưa có', following:'▶ đang theo', footer:'một ví · trực tiếp · không phải lời khuyên tài chính', default:'mặc định', triggers:'kích hoạt' },
  th: { equity:'ทุน', today:'วันนี้', open_positions:'สถานะเปิด', last_tick:'อัปเดตล่าสุด', no_scan_yet:'ยังไม่สแกน', equity_curve:'เส้นทุน', recent_closes:'ปิดล่าสุด', live_activity:'กิจกรรมสด', none:'ไม่มี', none_yet:'ยังไม่มี', following:'▶ ติดตาม', footer:'หนึ่งวอลเล็ต · สด · ไม่ใช่คำแนะนำทางการเงิน', default:'ค่าเริ่มต้น', triggers:'ทริกเกอร์' },
};
function applyI18n() {
  const dict = I18N[langState] || I18N.en;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const k = el.dataset.i18n;
    if (dict[k]) el.textContent = dict[k];
  });
}

async function loadRates() {
  const cached = JSON.parse(localStorage.getItem('hermes-fx') || 'null');
  if (cached && Date.now() - cached.t < 3600000) return cached.rates;
  try {
    const r = await fetch('https://open.er-api.com/v6/latest/USD');
    const d = await r.json();
    if (d.result !== 'success') throw new Error('rate fetch failed');
    const rates = { USD: 1, ...d.rates };
    localStorage.setItem('hermes-fx', JSON.stringify({ t: Date.now(), rates }));
    return rates;
  } catch (e) {
    return cached?.rates || { USD: 1 };
  }
}

// ── HERMES.HAMSTER habitat ──
// Tamagotchi-meets-Nous: hamster contemplates the digital rain. Stats are
// recomputed from the dashboard summary; cryptic quotes rotate on a timer.
// Law-of-attraction style money-magnet affirmations. The rabbit speaks
// them to the operator like a tiny Tamagotchi shaman — half meme, half
// belief-shifting. A few keep the matrix/rabbit flavor as easter eggs.
const HAMSTER_QUOTES = [
  'money flows to me effortlessly',
  'i am a magnet for wealth',
  'abundance is my birthright',
  'every trade aligns with prosperity',
  'the market loves me back',
  'i deserve massive gains',
  'wealth is my natural state',
  'the universe conspires for my profit',
  'infinite abundance flows through me',
  'every loss is a setup for a bigger win',
  'i trust the process',
  'green candles are drawn to me',
  'compound growth is inevitable',
  'i receive easily and abundantly',
  'my account grows on autopilot',
  '100x is just the beginning',
  'winning is my baseline',
  'the chain blesses my positions',
  'i am open to receiving more',
  'success is already on its way',
  'i attract the right setups',
  'gratitude multiplies my returns',
  'follow the white rabbit',
  'down the rabbit hole to riches',
];
let hamsterQuoteIdx = -1;
function rotateHamsterQuote() {
  const el = document.getElementById('hamster-quote');
  if (!el) return;
  hamsterQuoteIdx = (hamsterQuoteIdx + 1) % HAMSTER_QUOTES.length;
  el.style.opacity = '0';
  setTimeout(() => { el.textContent = HAMSTER_QUOTES[hamsterQuoteIdx]; el.style.opacity = '1'; }, 250);
}
// Hamster reacts to live trading events: execute → celebrate (yellow ⚡),
// dsl_exit profit → victory wiggle (green 💰), dsl_exit loss → defeat shake
// (red 💀). Animation classes auto-clear so subsequent events restart cleanly.
function triggerHamsterReaction(eventType, pnlPct) {
  const body = document.querySelector('.hamster-body');
  const habitat = document.querySelector('.habitat');
  if (!body || !habitat) return;
  body.classList.remove('celebrate', 'victory', 'defeat');
  habitat.classList.remove('flash-yellow', 'flash-green', 'flash-red');
  let bodyClass, habitatClass, burstChar;
  if (eventType === 'execute') {
    bodyClass = 'celebrate'; habitatClass = 'flash-yellow'; burstChar = '⚡';
  } else if (eventType === 'dsl_exit') {
    if ((pnlPct ?? 0) >= 0) { bodyClass = 'victory'; habitatClass = 'flash-green'; burstChar = '💰'; }
    else { bodyClass = 'defeat'; habitatClass = 'flash-red'; burstChar = '💀'; }
  } else { return; }
  // Force reflow so the same class re-fires on rapid repeat events.
  void body.offsetWidth;
  body.classList.add(bodyClass);
  habitat.classList.add(habitatClass);
  // Floating burst icon above the hamster
  const burst = document.createElement('span');
  burst.className = 'habitat-burst show';
  burst.textContent = burstChar;
  habitat.appendChild(burst);
  setTimeout(() => burst.remove(), 1200);
  setTimeout(() => {
    body.classList.remove(bodyClass);
    habitat.classList.remove(habitatClass);
  }, 1500);
}

// ── KPIs ──
// 8-bit pixel sprites: the always-visible white cat (heterochromia — green
// left eye, blue right eye) + a worried-mood face. Other PnL faces still
// use modern emoji. shape-rendering=crispEdges keeps the pixels sharp.
const SPRITE_RABBIT = `<svg viewBox="0 0 16 16" shape-rendering="crispEdges" xmlns="http://www.w3.org/2000/svg"><rect x="4" y="1" width="1" height="1" fill="#f5f5f5"/><rect x="3" y="2" width="3" height="1" fill="#f5f5f5"/><rect x="2" y="3" width="4" height="1" fill="#f5f5f5"/><rect x="11" y="1" width="1" height="1" fill="#f5f5f5"/><rect x="10" y="2" width="3" height="1" fill="#f5f5f5"/><rect x="10" y="3" width="4" height="1" fill="#f5f5f5"/><rect x="4" y="3" width="1" height="1" fill="#fbcfe8"/><rect x="11" y="3" width="1" height="1" fill="#fbcfe8"/><rect x="2" y="4" width="12" height="9" fill="#f5f5f5"/><rect x="4" y="7" width="2" height="2" fill="#15803d"/><rect x="10" y="7" width="2" height="2" fill="#3b82f6"/><rect x="7" y="10" width="2" height="1" fill="#f472b6"/><rect x="0" y="10" width="2" height="1" fill="#a3a3a3"/><rect x="14" y="10" width="2" height="1" fill="#a3a3a3"/><rect x="3" y="13" width="2" height="1" fill="#d4d4d4"/><rect x="11" y="13" width="2" height="1" fill="#d4d4d4"/></svg>`;
const SPRITE_WORRIED = `<svg viewBox="0 0 16 16" shape-rendering="crispEdges" xmlns="http://www.w3.org/2000/svg"><rect x="4" y="2" width="8" height="1" fill="#fde047"/><rect x="3" y="3" width="10" height="1" fill="#fde047"/><rect x="2" y="4" width="12" height="8" fill="#fde047"/><rect x="3" y="12" width="10" height="1" fill="#fde047"/><rect x="4" y="13" width="8" height="1" fill="#fde047"/><rect x="4" y="6" width="1" height="1" fill="#000"/><rect x="5" y="5" width="2" height="1" fill="#000"/><rect x="9" y="5" width="2" height="1" fill="#000"/><rect x="11" y="6" width="1" height="1" fill="#000"/><rect x="5" y="11" width="6" height="1" fill="#000"/><rect x="4" y="10" width="1" height="1" fill="#000"/><rect x="11" y="10" width="1" height="1" fill="#000"/><rect x="13" y="3" width="1" height="1" fill="#60a5fa"/><rect x="12" y="4" width="1" height="2" fill="#60a5fa"/><rect x="13" y="4" width="1" height="2" fill="#7dd3fc"/><rect x="13" y="6" width="1" height="1" fill="#60a5fa"/></svg>`;

// Agent-pet mood: status sets the base, then PnL nudges it. Big winners → 🤑,
// big losers → 😱. The pet element gets a CSS modifier class for animation
// (shake on executing, sleep on offline/stale, default gentle bounce otherwise).
function updatePet(status, dailyPnlPct) {
  const pet = document.getElementById('pet');
  if (!pet) return;
  let face = '🤖', mood = '', isSprite = false;
  if (status === 'offline') { face = '💤'; mood = 'sleep'; }
  else if (status === 'stale') { face = '😴'; mood = 'sleep'; }
  else if (status === 'executing') { face = '⚡'; mood = 'shake'; }
  else if (status === 'scanning') { face = '👀'; }
  // PnL overrides for strong signals
  if (status !== 'offline' && status !== 'stale') {
    if (dailyPnlPct >= 5) face = '🤑';
    else if (dailyPnlPct <= -5) face = '😱';
    else if (dailyPnlPct >= 1.5) face = '😎';
    else if (dailyPnlPct <= -1.5) { face = SPRITE_WORRIED; isSprite = true; }
  }
  if (isSprite) pet.innerHTML = face;
  else pet.textContent = face;
  pet.className = 'pet' + (mood ? ' ' + mood : '') + (isSprite ? ' pet-sprite' : '');
}

async function refreshSummary() {
  try {
    const r = await fetch('/api/dashboard/summary');
    const s = await r.json();
    document.getElementById('kpi-equity').innerHTML = dollarHTML(fmtMoney(s.equity));
    // Per-dex breakdown: e.g. "main $76 + xyz $62 + km $15 = $153 free"
    const brk = document.getElementById('kpi-equity-breakdown');
    if (brk) {
      const dexAvail = s.dex_available || {};
      const parts = [];
      let total = 0;
      // Order: main first, then non-zero dexes alphabetically
      const dexNames = Object.keys(dexAvail).sort((a, b) => {
        if (a === '') return -1;
        if (b === '') return 1;
        return a.localeCompare(b);
      });
      for (const d of dexNames) {
        const v = Number(dexAvail[d] || 0);
        if (v < 0.5 && d !== '') continue;
        const label = d === '' ? 'main' : d;
        parts.push(`${label} ${fmtMoney(v)}`);
        total += v;
      }
      brk.textContent = parts.length > 1
        ? `${parts.join(' + ')} = $${total.toFixed(2)} free`
        : (parts.length === 1 ? `$${total.toFixed(2)} free` : '');
    }
    const pnlEl = document.getElementById('kpi-pnl');
    pnlEl.innerHTML = dollarHTML(fmtMoney(s.daily_pnl));
    pnlEl.className = 'text-2xl font-bold num ' + (s.daily_pnl >= 0 ? 'text-emerald-400' : 'text-red-400');
    document.getElementById('kpi-pnl-pct').textContent = fmtPct(s.daily_pnl_pct);
    document.getElementById('kpi-open').textContent = s.open_positions;
    document.getElementById('kpi-tick').textContent = fmtAge(s.last_tick_age_s);
    document.getElementById('kpi-tick-detail').textContent = s.last_scan_triggers + ' triggers';
    const pill = document.getElementById('status-pill');
    pill.textContent = s.status;
    pill.className = 'pill ' + s.status;
    updatePet(s.status, s.daily_pnl_pct);
  } catch (e) {}
}

// ── Positions ──
let currentPosSort = 'pnl_desc';
async function refreshPositions() {
  try {
    const r = await fetch('/api/dashboard/positions');
    const ps = await r.json();
    const el = document.getElementById('positions');
    if (!ps.length) { el.innerHTML = '<div class="text-zinc-500 text-xs">none</div>'; return; }
    if (currentPosSort === 'pnl_desc') ps.sort((a, b) => (b.unrealized_pct ?? 0) - (a.unrealized_pct ?? 0));
    else if (currentPosSort === 'pnl_asc') ps.sort((a, b) => (a.unrealized_pct ?? 0) - (b.unrealized_pct ?? 0));
    el.innerHTML = ps.map(p => {
      const pnlColor = p.unrealized_pct >= 0 ? 'text-emerald-400' : 'text-red-400';
      const sideTag = p.side === 'long'
        ? '<span class="text-[10px] text-emerald-400 font-semibold">LONG</span>'
        : '<span class="text-[10px] text-red-400 font-semibold">SHORT</span>';
      const levTag = p.leverage > 1 ? `<span class="text-[10px] text-zinc-500">${p.leverage}x</span>` : '';
      const sizeFmt = p.size >= 1 ? p.size.toFixed(2) : p.size.toFixed(4);
      const pxFmt = (v) => v < 1 ? v.toFixed(5) : v < 100 ? v.toFixed(3) : v.toFixed(2);
      const floor = p.dsl?.floor_px ? ('floor ' + pxFmt(p.dsl.floor_px)) : '<span class="text-zinc-700">no DSL</span>';
      const phase = p.dsl?.phase || '';
      const usd = p.unrealized_pnl_usd;
      const usdStr = `<span class="dollar-value">${fmtMoney(usd, { signed: true })}</span><span class="dollar-mask">•••</span>`;
      const spotNote = p.leverage > 1
        ? `<span class="text-zinc-600 text-[10px] ml-1" title="spot ${p.spot_pct >= 0 ? '+' : ''}${p.spot_pct.toFixed(2)}% × ${p.leverage}x leverage = ROE shown">(spot ${p.spot_pct >= 0 ? '+' : ''}${p.spot_pct.toFixed(2)}%)</span>`
        : '';
      return `<div class="grid grid-cols-12 gap-2 py-1 border-b border-zinc-800 last:border-0 num text-xs items-center">
        <div class="col-span-2 flex items-baseline gap-2"><span class="font-bold text-sm">${p.coin}</span>${sideTag} ${levTag}</div>
        <div class="col-span-2 text-zinc-400">${sizeFmt} @ ${pxFmt(p.entry_px)}</div>
        <div class="col-span-2 text-zinc-400">mark ${pxFmt(p.mark_px)}</div>
        <div class="col-span-3 ${pnlColor} text-sm font-semibold">${usdStr} (${p.unrealized_pct >= 0 ? '+' : ''}${p.unrealized_pct.toFixed(1)}%)${spotNote}</div>
        <div class="col-span-3 text-zinc-500 text-[11px]">${floor} ${phase}</div>
      </div>`;
    }).join('');
  } catch (e) {}
}

// ── Closed trades ──
async function refreshCloses() {
  try {
    const r = await fetch('/api/dashboard/closed-trades?limit=20');
    const cs = await r.json();
    const el = document.getElementById('closes');
    if (!cs.length) { el.innerHTML = '<div class="text-zinc-500 text-xs">none yet</div>'; return; }
    el.innerHTML = cs.map(c => {
      const ageMin = Math.max(0, Math.round((Date.now() - c.ts) / 60000));
      const ageStr = ageMin < 60 ? ageMin + 'm ago' : ageMin < 1440 ? Math.floor(ageMin/60) + 'h ago' : Math.floor(ageMin/1440) + 'd ago';
      const pnlColor = c.pnl_pct >= 0 ? 'text-emerald-400' : 'text-red-400';
      const sideTag = c.side === 'long' ? '<span class="text-emerald-400 text-[10px] font-semibold">LONG</span>'
                    : c.side === 'short' ? '<span class="text-red-400 text-[10px] font-semibold">SHORT</span>'
                    : '<span class="text-zinc-500 text-[10px]">—</span>';
      const levMark = c.leverage_estimated ? '~' : '';
      const levTag = c.leverage > 1 ? `<span class="text-zinc-500 text-[10px]" title="${c.leverage_estimated ? 'leverage estimated from HL per-coin max — not recorded for this old trade' : ''}">${levMark}${c.leverage}x</span>` : '';
      const sourceTag = c.source === 'dsl' ? '<span class="text-amber-400 text-[10px]">dsl</span>'
                                           : '<span class="text-zinc-500 text-[10px]">manual</span>';
      const failedTag = c.executed ? '' : ' <span class="text-red-400 text-[10px]">FAILED</span>';
      const pnlExactMark = c.pnl_source === 'fill' ? '' : '~';
      const tipLines = [
        `spot move × ${c.leverage}x = ${c.pnl_pct_gross.toFixed(2)}% gross`,
        `minus ${c.fees_pct.toFixed(2)}% taker fees`,
        `= ${c.pnl_pct.toFixed(2)}% net`,
        c.pnl_source === 'fill' ? `realized at fill ${c.fill_px} (entry ${c.entry_px})` : 'estimated from DSL trigger mark (no fill captured)',
      ].join(' · ');
      const spotNote = c.spot_pct && c.leverage > 1
        ? `<span class="text-zinc-600 text-[10px] ml-1" title="${tipLines}">(spot ${c.spot_pct >= 0 ? '+' : ''}${c.spot_pct.toFixed(2)}%)</span>` : '';
      return `<div class="grid grid-cols-12 gap-2 py-1 border-b border-zinc-800 last:border-0 num text-xs items-center">
        <div class="col-span-2 flex items-baseline gap-2"><span class="font-bold text-sm">${c.coin}</span>${sideTag} ${levTag}</div>
        <div class="col-span-5 text-zinc-400 truncate" title="${c.reason}">${c.reason}${failedTag}</div>
        <div class="col-span-3 ${pnlColor} text-sm font-semibold">${pnlExactMark}${c.pnl_pct >= 0 ? '+' : ''}${c.pnl_pct.toFixed(1)}%${spotNote}</div>
        <div class="col-span-1 text-zinc-500">${sourceTag}</div>
        <div class="col-span-1 text-zinc-500 text-right">${ageStr}</div>
      </div>`;
    }).join('');
    const dslOnly = cs.filter(c => c.source === 'dsl');
    const wins = dslOnly.filter(c => c.pnl_pct > 0).length;
    const total = dslOnly.length;
    if (total > 0) {
      const avgPnl = (dslOnly.reduce((a,c)=>a+c.pnl_pct,0) / total).toFixed(1);
      document.getElementById('closes-stats').textContent = `${wins}/${total} winners · avg ${avgPnl >= 0 ? '+' : ''}${avgPnl}% (leveraged)`;
    }
  } catch (e) {}
}

// ── Equity curve ──
let chart;
let currentRange = 86400;
const RANGE_UNIT = {86400: 'hour', 604800: 'day', 2592000: 'day'};

function makeGradient(ctx, area) {
  const g = ctx.createLinearGradient(0, area.top, 0, area.bottom);
  g.addColorStop(0,   'rgba(16, 185, 129, 0.28)');
  g.addColorStop(0.6, 'rgba(16, 185, 129, 0.06)');
  g.addColorStop(1,   'rgba(16, 185, 129, 0)');
  return g;
}

async function refreshChart(rangeSec) {
  currentRange = rangeSec;
  try {
    const r = await fetch('/api/dashboard/equity-curve?range_s=' + rangeSec);
    const series = await r.json();
    const data = series.map(p => ({x: p.ts, y: p.equity}));
    const empty = document.getElementById('equity-empty');
    empty.classList.toggle('hidden', data.length > 0);
    if (!chart) {
      const ctx = document.getElementById('equity-chart').getContext('2d');
      chart = new Chart(ctx, {
        type: 'line',
        data: { datasets: [{
          data,
          borderColor: '#34d399',
          borderWidth: 1.75,
          borderJoinStyle: 'round',
          borderCapStyle: 'round',
          cubicInterpolationMode: 'monotone',
          tension: 0.35,
          pointRadius: 0,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: '#34d399',
          pointHoverBorderColor: '#0a0a0a',
          pointHoverBorderWidth: 2,
          fill: true,
          backgroundColor: (c) => {
            const {ctx, chartArea} = c.chart;
            if (!chartArea) return 'rgba(16,185,129,0.1)';
            return makeGradient(ctx, chartArea);
          },
        }] },
        options: {
          responsive: true, animation: false, parsing: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { display: false },
            decimation: { enabled: true, algorithm: 'lttb', samples: 80, threshold: 100 },
            tooltip: {
              backgroundColor: '#18181b', borderColor: '#27272a', borderWidth: 1,
              titleColor: '#a1a1aa', bodyColor: '#e5e5e5', padding: 8, displayColors: false,
              callbacks: {
                title: (items) => new Date(items[0].parsed.x).toLocaleString(),
                label: (item) => maskDollar(fmtMoney(item.parsed.y)),
              }
            }
          },
          scales: {
            x: {
              type: 'time', time: { unit: RANGE_UNIT[rangeSec] || 'hour' },
              ticks: { color: '#52525b', maxTicksLimit: 6, font: { size: 10 } },
              grid: { display: false }, border: { display: false },
            },
            y: {
              ticks: { color: '#52525b', callback: v => isDiscreet() ? '•••' : fmtMoney(v), font: { size: 10 }, maxTicksLimit: 6 },
              grid: { color: '#18181b', drawTicks: false }, border: { display: false },
            },
          }
        }
      });
    } else {
      chart.data.datasets[0].data = data;
      chart.options.scales.x.time.unit = RANGE_UNIT[rangeSec] || 'hour';
      chart.update('none');
    }
  } catch (e) { console.error('chart error', e); }
}
document.querySelectorAll('.range-btn').forEach(b => {
  b.addEventListener('click', () => {
    refreshChart(parseInt(b.dataset.range));
    document.querySelectorAll('.range-btn').forEach(x => x.classList.remove('bg-emerald-700'));
    b.classList.add('bg-emerald-700');
  });
});
document.querySelectorAll('.pos-sort-btn').forEach(b => {
  b.addEventListener('click', () => {
    currentPosSort = b.dataset.sort;
    document.querySelectorAll('.pos-sort-btn').forEach(x => { x.classList.remove('bg-emerald-700'); x.classList.add('bg-zinc-800'); });
    b.classList.remove('bg-zinc-800');
    b.classList.add('bg-emerald-700');
    refreshPositions();
  });
});

// ── Live feed (SSE) ──
function fmtPx(v) { if (v == null) return '?'; return v < 1 ? v.toFixed(5) : v < 100 ? v.toFixed(3) : v.toFixed(2); }

function renderEvent(e) {
  const ts = fmtTime(e.ts || Date.now());
  const ev = e.event || '?';
  let glyph = '?', text = '', cls = ev, detail = '', tooltip = '';
  if (ev === 'loop_heartbeat') {
    glyph = '♥'; cls = 'heartbeat';
    let cfgStr = '';
    if (e.config) {
      const c = e.config;
      // Compact config snippet: frac×lev N/cap cool=Nm conf=N hip3:on
      const frac = (c.frac != null ? (c.frac * 100).toFixed(1) + '%' : '?');
      const lev = (c.lev != null ? c.lev + 'x' : '?');
      const conc = (c.max_conc != null ? c.max_conc : '?');
      const cap = (c.notional_cap != null ? c.notional_cap + 'x' : '?');
      const cool = (c.cool_min != null ? c.cool_min + 'm' : '?');
      const conf = (c.min_conf != null ? c.min_conf : '?');
      const kill = (c.kill != null ? '$' + c.kill : '?');
      const hip3 = c.hip3 ? 'on' : 'off';
      const crypto = c.crypto === false ? 'off' : 'on';
      cfgStr = `  ⚙ ${frac}×${lev} slots=${conc} cap=${cap} cool=${cool} conf=${conf} kill=${kill} crypto:${crypto} hip3:${hip3}`;
    }
    text = `perp=${maskDollar('$'+(e.equity||0).toFixed(2))} avail=${maskDollar('$'+(e.available||0).toFixed(2))} daily=${maskDollar(((e.daily_pnl||0)>=0?'+':'')+'$'+(e.daily_pnl||0).toFixed(2))} open=${e.open_positions||0}${cfgStr}`;
  } else if (ev === 'loop_start') {
    glyph = '▶'; cls = 'scan';
    // Show key knobs on startup so it's obvious what the bot is configured to do
    const c = e.config || {};
    const frac = c.equity_fraction_per_trade != null ? (c.equity_fraction_per_trade * 100).toFixed(1) + '%' : '?';
    const lev = c.leverage != null ? c.leverage + 'x' : '?';
    const conc = c.max_concurrent != null ? c.max_concurrent : '?';
    const cap = c.max_total_notional_pct != null ? c.max_total_notional_pct + 'x' : '?';
    const cool = c.cooldown_min != null ? c.cooldown_min + 'm' : '?';
    const conf = c.min_ai_confidence != null ? c.min_ai_confidence : '?';
    const hip3 = c.enable_hip3 ? 'on' : 'off';
    const crypto = c.enable_crypto === false ? 'off' : 'on';
    const mode = c.mode || '?';
    text = `loop_start interval=${e.scan_interval||60}s min_score=${e.min_score||20}  ⚙ mode=${mode} ${frac}×${lev} slots=${conc} cap=${cap} cool=${cool} conf=${conf} crypto:${crypto} hip3:${hip3}`;
  } else if (ev === 'scan') {
    glyph = '•'; cls = 'scan';
    // Prefer scored coin list if present (newer events); fall back to plain names.
    const scored = e.coin_scores || [];
    const coinsStr = scored.length
      ? scored.slice(0, 6).map(c => `${c.coin}(${c.score})`).join(', ') + (scored.length > 6 ? ` (+${scored.length-6})` : '')
      : ((e.coins || []).slice(0, 6).join(', ') + (e.coins?.length > 6 ? ` (+${e.coins.length-6})` : ''));
    text = `scan       ${e.triggers||0} triggers${coinsStr ? ' — ' + coinsStr : ''}`;
    if (scored.length) tooltip = scored.map(c => `${c.coin}: score ${c.score}` + (c.triggers?.length ? ` [${c.triggers.join(', ')}]` : '')).join('\\n');
  } else if (ev === 'ta_skip') {
    glyph = '✗'; cls = 'scan';
    const scoreNote = e.score != null ? ` ta=${e.score}` : '';
    const trigNote = e.trigger_score != null ? ` trig=${e.trigger_score}` : '';
    text = `ta_skip    ${e.coin} (${e.signal})${scoreNote}${trigNote}`;
  } else if (ev === 'research') {
    glyph = '?'; cls = 'research';
    text = `research   ${e.coin} → ${e.verdict} (conf ${e.confidence})`;
    // Inline preview of reasoning + entry/stop/tp when present.
    const priceTriad = (e.entry_px || e.stop_px || e.tp_px)
      ? ` · entry ${fmtPx(e.entry_px)}/sl ${fmtPx(e.stop_px)}/tp ${fmtPx(e.tp_px)}` : '';
    // Show the FULL reasoning — no truncation. The feed wraps long lines.
    detail = priceTriad + (e.reasoning ? ` — ${e.reasoning}` : '');
  } else if (ev === 'execute') {
    const ok = e.executed;
    glyph = ok ? '✓' : '✗'; cls = ok ? 'execute' : 'execute-fail';
    if (ok) {
      const sz = e.size_usd != null ? ' ' + maskDollar('$'+e.size_usd.toFixed(2)) : '';
      const ep = e.entry_px != null ? ` @ ${fmtPx(e.entry_px)}` : '';
      text = `execute    ${e.coin} ${e.side || '?'}${sz}${ep}  ${e.detail || ''}`;
      if (e.stop_px || e.tp_px) tooltip = `entry ${fmtPx(e.entry_px)}\nstop ${fmtPx(e.stop_px)}\ntp ${fmtPx(e.tp_px)}\nsize $${(e.size_usd||0).toFixed(2)}\norder ${e.detail || ''}`;
    } else {
      const blocked = Array.isArray(e.blocked_by) ? e.blocked_by.join(' · ') : (e.blocked_by || e.detail || '');
      text = `execute    ${e.coin} ${e.side || '?'}  BLOCKED: ${blocked}`;
      if (Array.isArray(e.blocked_by) && e.blocked_by.length > 1) tooltip = e.blocked_by.join('\\n');
    }
  } else if (ev === 'dsl_exit') {
    glyph = '⏹'; cls = 'dsl_exit';
    const side = e.side ? `${e.side} ` : '';
    const lev = e.leverage ? `${e.leverage}x ` : '';
    const pnlPct = e.realized_pnl_pct != null ? e.realized_pnl_pct : (e.unrealized_pct || 0);
    text = `dsl_exit   ${e.coin} ${side}${lev} ${e.reason}  (${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%)`;
    if (e.fill_px) tooltip = `entry ${fmtPx(e.entry_px)}\nfill ${fmtPx(e.fill_px)}\nspot ${(e.realized_spot_pct||0).toFixed(2)}%\nleveraged ${(pnlPct||0).toFixed(2)}%`;
  } else if (ev === 'ai_close') {
    // AI CLOSE verdict acted on (position closed because structure flipped).
    glyph = e.executed ? '⏹' : '✗';
    cls = e.executed ? 'dsl_exit' : 'execute-fail';
    const status = e.executed ? 'closed' : 'close FAILED';
    text = `ai_close   ${e.coin} ${status}`;
    detail = e.reasoning ? ` — ${e.reasoning}` : '';
  } else if (ev === 'error') {
    glyph = '!'; cls = 'error';
    text = `error      ${e.coin || e.scope || 'loop'}: ${(e.error || '').slice(0, 120)}`;
  } else if (ev === 'loop_stop') {
    glyph = '■'; text = `loop_stop`;
  } else {
    text = `${ev}      ${JSON.stringify({...e, ts: undefined, event: undefined})}`;
  }
  const row = document.createElement('div');
  row.className = 'feed-row ' + cls;
  row.textContent = `[${ts}] ${glyph}  ${text}${detail}`;
  if (tooltip) row.title = tooltip;
  return row;
}

const feed = document.getElementById('feed');
const es = new EventSource('/api/feed/stream');
es.onmessage = (m) => {
  try {
    const e = JSON.parse(m.data);
    feed.appendChild(renderEvent(e));
    while (feed.childNodes.length > 200) feed.removeChild(feed.firstChild);
    feed.scrollTop = feed.scrollHeight;
    // Hamster reacts to executes (successful only) and DSL exits
    if (e.event === 'execute' && e.executed) {
      triggerHamsterReaction('execute');
    } else if (e.event === 'dsl_exit') {
      const pnlPct = e.realized_pnl_pct != null ? e.realized_pnl_pct : (e.unrealized_pct || 0);
      triggerHamsterReaction('dsl_exit', pnlPct);
    }
  } catch {}
};

// ── locale init + change handlers ──
async function initLocale() {
  const rates = await loadRates();
  const savedCcy = localStorage.getItem('hermes-ccy') || 'USD';
  const savedLang = localStorage.getItem('hermes-lang') || 'en';
  ccyState = { code: savedCcy, rate: rates[savedCcy] || 1 };
  langState = savedLang;
  const ccySel = document.getElementById('ccy-sel');
  const langSel = document.getElementById('lang-sel');
  if (ccySel) ccySel.value = savedCcy;
  if (langSel) langSel.value = savedLang;
  applyI18n();
}
document.getElementById('ccy-sel')?.addEventListener('change', async (e) => {
  const code = e.target.value;
  localStorage.setItem('hermes-ccy', code);
  const rates = await loadRates();
  ccyState = { code, rate: rates[code] || 1 };
  refreshSummary(); refreshPositions(); refreshCloses();
  if (chart) refreshChart(currentRange);
});
document.getElementById('lang-sel')?.addEventListener('change', (e) => {
  langState = e.target.value;
  localStorage.setItem('hermes-lang', langState);
  applyI18n();
});

// ── Highlight the active page in the primary navbar + carry the operator
// token across navigation. Without the token-carry, clicking OPERATOR after
// entering operator mode on / would land on a 401-locked operator page.
(function(){
  const here = window.location.pathname.replace(/[/]$/, '') || '/';
  const tok = new URLSearchParams(window.location.search).get('token')
           || localStorage.getItem('hermes-op-token') || '';
  document.querySelectorAll('a[data-nav]').forEach(a => {
    if (a.dataset.nav === here) a.classList.add('nav-active');
    if (tok) {
      const u = new URL(a.href, window.location.origin);
      u.searchParams.set('token', tok);
      a.href = u.toString();
    }
  });
})();

// ── Operator-mode toggle: lets the user paste their HERMES_OPERATOR_TOKEN
// without hand-editing the URL. Token persists to localStorage so subsequent
// page loads stay unlocked without retyping. Click again to clear.
(function () {
  const btn = document.getElementById('operator-toggle');
  if (!btn) return;
  function syncBtn() {
    const tok = localStorage.getItem('hermes-op-token') || new URLSearchParams(window.location.search).get('token') || '';
    btn.textContent = tok ? '🔓 op' : '🔒 op';
    btn.title = tok
      ? 'operator mode ON — click to clear token (revert to read-only)'
      : 'enter operator mode — unlocks Hermes terminal (Cmd+K)';
  }
  syncBtn();
  btn.addEventListener('click', () => {
    const current = localStorage.getItem('hermes-op-token') || '';
    if (current) {
      if (confirm('Clear operator token and revert to read-only?')) {
        localStorage.removeItem('hermes-op-token');
        // Strip ?token= from URL for a clean reload
        const u = new URL(window.location.href);
        u.searchParams.delete('token');
        window.location.replace(u.toString());
      }
      return;
    }
    const tok = prompt('Paste your HERMES_OPERATOR_TOKEN:\\n(stored only in this browser via localStorage)');
    if (!tok || !tok.trim()) return;
    const clean = tok.trim();
    localStorage.setItem('hermes-op-token', clean);
    // Reload with ?token= so backend SSE / page-token-reading consumers pick it up.
    const u = new URL(window.location.href);
    u.searchParams.set('token', clean);
    window.location.replace(u.toString());
  });
})();

// ── Hermes terminal: Cmd+K (Ctrl+K) opens a command-center console. Routes
// the input to /api/dashboard/operator/terminal with the operator token read
// from the URL — built-in commands resolve locally; anything else falls
// through to Nous Hermes via the backend.
(function () {
  const modal = document.getElementById('hermes-modal');
  const input = document.getElementById('hermes-input');
  const history = document.getElementById('hermes-history');
  const closeBtn = document.getElementById('hermes-close');
  if (!modal || !input || !history) return;
  // Token resolution order: ?token= in URL wins, then localStorage, else empty.
  // Letting localStorage hold it means the operator doesn't have to retype
  // the token on every page load — but URL still wins so a copy-pasted
  // share-link can override.
  const tokenFromUrl = new URLSearchParams(window.location.search).get('token') || '';
  const tokenFromStore = localStorage.getItem('hermes-op-token') || '';
  const operatorToken = tokenFromUrl || tokenFromStore;
  if (tokenFromUrl) localStorage.setItem('hermes-op-token', tokenFromUrl);

  function appendLine(text, kind) {
    const div = document.createElement('div');
    div.className = 'hermes-line' + (kind ? ' hermes-' + kind : '');
    div.textContent = text;
    history.appendChild(div);
    history.scrollTop = history.scrollHeight;
  }
  function openModal() {
    modal.classList.remove('hidden');
    setTimeout(() => input.focus(), 30);
  }
  function closeModal() { modal.classList.add('hidden'); }

  async function send(cmd) {
    appendLine('▸ ' + cmd, 'cmd');
    if (!operatorToken) {
      appendLine('no operator token set — click 🔒 op in the top bar to enter one.', 'error');
      return;
    }
    try {
      const r = await fetch('/api/dashboard/operator/terminal?token=' + encodeURIComponent(operatorToken), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: cmd }),
      });
      if (!r.ok) {
        appendLine(`error ${r.status}: ${(await r.text()).slice(0, 200)}`, 'error');
        return;
      }
      const j = await r.json();
      appendLine(j.response || '(empty response)', j.kind || 'chat');
    } catch (e) {
      appendLine('network error: ' + e.message, 'error');
    }
  }

  // Cmd+K / Ctrl+K toggles. Esc closes.
  window.addEventListener('keydown', (ev) => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'k') {
      ev.preventDefault();
      modal.classList.contains('hidden') ? openModal() : closeModal();
    } else if (ev.key === 'Escape' && !modal.classList.contains('hidden')) {
      closeModal();
    }
  });
  modal.querySelector('.hermes-modal-bg')?.addEventListener('click', closeModal);
  closeBtn?.addEventListener('click', closeModal);

  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && input.value.trim()) {
      const cmd = input.value.trim();
      input.value = '';
      send(cmd);
    }
  });
})();

// ── discreet toggle: flip body.discreet, persist, re-render the views that
// build dollar text imperatively (KPIs, positions, chart) so they pick up
// the new state on the same tick rather than waiting for the next poll.
(function () {
  const btn = document.getElementById('discreet-toggle');
  if (!btn) return;
  // Restore persisted state
  if (localStorage.getItem('hermes-discreet') === '1') {
    document.body.classList.add('discreet');
    btn.textContent = '🙈';
  }
  btn.addEventListener('click', () => {
    const on = document.body.classList.toggle('discreet');
    localStorage.setItem('hermes-discreet', on ? '1' : '0');
    btn.textContent = on ? '🙈' : '👁';
    refreshSummary();
    refreshPositions();
    if (chart) chart.update('none');
  });
})();

// ── kickoff + polling ──
initLocale().then(() => {
  refreshSummary(); refreshPositions(); refreshCloses(); refreshChart(86400);
  rotateHamsterQuote();
});
setInterval(refreshSummary, 5000);
setInterval(refreshPositions, 15000);
setInterval(refreshCloses, 20000);
setInterval(() => refreshChart(currentRange), 60000);
setInterval(rotateHamsterQuote, 7000);
</script>
</body>
</html>
"""


_CONFIG_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Hermes Trader · 配置中心</title>
<!--
  LAN-safe: zero external CDN/font dependencies.
  All styles are inlined so the page loads on isolated local networks.
-->
<style>
  /* ===== Design tokens: eye-friendly dark palette, low-glare for long sessions ===== */
  :root {
    --bg:        #1b1e25;
    --surface:   #232730;
    --surface2:  #2a2f3a;
    --border:    #353b47;
    --border-soft:#2d323c;
    --text:      #d4d9e2;
    --text-2:    #9aa3b2;
    --text-3:    #6b7280;
    --accent:    #5b9df9;
    --accent-soft:rgba(91,157,249,.14);
    --success:   #4caf82;
    --success-soft:rgba(76,175,130,.14);
    --warn:      #d4a056;
    --warn-soft: rgba(212,160,86,.14);
    --danger:    #d46b6b;
    --danger-soft:rgba(212,107,107,.14);
    --radius:    10px;
    --radius-sm: 6px;
    --mono: ui-monospace,"SF Mono","Cascadia Code",Menlo,Consolas,"Liberation Mono",monospace;
    --han: "PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans SC","Source Han Sans SC","WenQuanYi Micro Hei",system-ui,sans-serif;
  }
  *,*::before,*::after{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{
    font-family:var(--han);
    background:var(--bg);
    color:var(--text);
    line-height:1.6;
    -webkit-font-smoothing:antialiased;
    -moz-osx-font-smoothing:grayscale;
  }
  .wrap{max-width:1180px;margin:0 auto;padding:24px 20px 48px}

  /* ===== Overview stat strip (错落有致的概览栏) ===== */
  .stat-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}
  .stat{
    background:var(--surface);border:1px solid var(--border-soft);
    border-radius:var(--radius);padding:14px 16px;position:relative;overflow:hidden;
  }
  .stat::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent)}
  .stat.s-warn::before{background:var(--warn)}
  .stat.s-ok::before{background:var(--success)}
  .stat.s-danger::before{background:var(--danger)}
  .stat:nth-child(2n){transform:translateY(6px)}
  .stat-label{font-size:11.5px;color:var(--text-3);font-weight:500;letter-spacing:.04em;text-transform:uppercase}
  .stat-value{font-size:20px;font-weight:600;margin-top:4px;font-family:var(--mono);letter-spacing:.01em}
  .stat-sub{font-size:11px;color:var(--text-3);margin-top:2px}

  /* ===== Section cards (分组卡片，错落阴影) ===== */
  .cfg-card{
    background:var(--surface);border:1px solid var(--border-soft);
    border-radius:var(--radius);padding:0;margin-bottom:16px;overflow:hidden;
    box-shadow:0 1px 3px rgba(0,0,0,.18);
  }
  .cfg-card:nth-child(even){box-shadow:0 3px 10px rgba(0,0,0,.12)}
  .cfg-card-head{
    display:flex;align-items:center;gap:10px;padding:13px 18px;cursor:pointer;
    background:linear-gradient(180deg,var(--surface2),var(--surface));
    border-bottom:1px solid var(--border-soft);user-select:none;
  }
  .cfg-card-head .chev{font-size:9px;color:var(--text-3);transition:transform .15s}
  .cfg-card.collapsed .chev{transform:rotate(-90deg)}
  .cfg-card.collapsed .cfg-card-body{display:none}
  .cfg-card-title{font-size:14px;font-weight:600;color:var(--text)}
  .cfg-card-count{margin-left:auto;font-size:11px;color:var(--text-3);font-family:var(--mono)}
  .cfg-card-body{padding:6px 18px 14px}

  /* ===== Nested field hint (嵌套字段中文提示) ===== */
  .nested-hint{
    border:1px dashed var(--border);border-radius:var(--radius-sm);
    background:rgba(0,0,0,.15);padding:10px 12px;margin:6px 0 4px;
  }
  .nested-hint summary{
    cursor:pointer;font-size:12px;color:var(--accent);font-weight:600;
    list-style:none;display:flex;align-items:center;gap:8px;
  }
  .nested-hint summary::-webkit-details-marker{display:none}
  .nested-hint summary::before{content:"\25B6";font-size:8px;transition:transform .15s}
  .nested-hint[open] summary::before{transform:rotate(90deg)}
  .field-map{margin-top:8px;display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:4px 16px}
  .field-map .fm{display:flex;gap:6px;font-size:11.5px;line-height:1.4}
  .field-map .fm code{font-family:var(--mono);color:var(--accent);white-space:nowrap}
  .field-map .fm span{color:var(--text-2)}

  /* ===== Header ===== */
  header{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:20px}
  .brand{display:flex;align-items:center;gap:12px}
  .brand-mark{
    width:38px;height:38px;border-radius:10px;
    background:linear-gradient(135deg,var(--accent),#7c5cff);
    display:flex;align-items:center;justify-content:center;
    font-weight:700;color:#fff;font-size:18px;flex-shrink:0;
    box-shadow:0 4px 14px rgba(91,157,249,.25);
  }
  .brand-title{font-size:18px;font-weight:600;letter-spacing:.02em}
  .brand-sub{font-size:12px;color:var(--text-3);font-family:var(--mono);letter-spacing:.04em}

  /* ===== Nav ===== */
  nav{display:flex;gap:6px;margin-bottom:22px;flex-wrap:wrap;background:var(--surface);padding:6px;border-radius:var(--radius);border:1px solid var(--border-soft)}
  .nav-link{
    padding:8px 16px;font-size:13px;font-weight:500;color:var(--text-2);
    text-decoration:none;border-radius:var(--radius-sm);transition:all .15s ease;
    font-family:var(--han);
  }
  .nav-link:hover{color:var(--text);background:var(--surface2)}
  .nav-link.nav-active{color:#fff;background:var(--accent);box-shadow:0 2px 8px rgba(91,157,249,.3)}

  /* ===== Cards ===== */
  .card{background:var(--surface);border:1px solid var(--border-soft);border-radius:var(--radius);padding:20px;margin-bottom:18px}
  .card-title{font-size:13px;color:var(--text-3);margin:0 0 14px;font-weight:500;display:flex;align-items:center;gap:8px}
  .card-title .dot{width:7px;height:7px;border-radius:50%;background:var(--success);box-shadow:0 0 8px var(--success)}

  /* ===== Token bar ===== */
  .token-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .token-bar label{font-size:13px;color:var(--text-2);white-space:nowrap;font-weight:500}
  .token-bar input{
    flex:1;min-width:220px;background:var(--bg);border:1px solid var(--border);
    color:var(--text);font-family:var(--mono);font-size:13px;padding:9px 12px;
    border-radius:var(--radius-sm);outline:none;transition:border-color .15s,box-shadow .15s;
  }
  .token-bar input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
  .auth-badge{font-size:11px;padding:4px 10px;border-radius:20px;font-weight:600;letter-spacing:.03em;white-space:nowrap}
  .auth-badge.on{color:var(--success);background:var(--success-soft);border:1px solid rgba(76,175,130,.3)}
  .auth-badge.off{color:var(--text-3);background:var(--surface2);border:1px solid var(--border)}

  /* ===== Toolbar ===== */
  .toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:18px}
  .toolbar-meta{font-size:12px;color:var(--text-3);font-family:var(--mono)}
  .toolbar-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .cfg-mode{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;font-size:12px;font-weight:600;border-radius:20px;border:1px solid}
  .cfg-mode.LIVE{color:var(--success);background:var(--success-soft);border-color:rgba(76,175,130,.35)}
  .cfg-mode.OFF{color:var(--warn);background:var(--warn-soft);border-color:rgba(212,160,86,.35)}

  /* ===== Buttons ===== */
  .btn{
    font-family:var(--han);font-size:13px;font-weight:500;padding:8px 16px;
    border-radius:var(--radius-sm);border:1px solid transparent;cursor:pointer;
    transition:all .15s ease;white-space:nowrap;
  }
  .btn:disabled{opacity:.38;cursor:not-allowed}
  .btn-save{background:var(--accent);color:#fff;border-color:var(--accent)}
  .btn-save:hover:not(:disabled){background:#4a8ce8;border-color:#4a8ce8}
  .btn-reset{background:transparent;color:var(--warn);border-color:rgba(212,160,86,.4)}
  .btn-reset:hover:not(:disabled){background:var(--warn-soft)}
  .btn-rollback{background:transparent;color:var(--danger);border-color:rgba(212,107,107,.4)}
  .btn-rollback:hover:not(:disabled){background:var(--danger-soft)}
  .btn-history{background:transparent;color:var(--text-2);border-color:var(--border)}
  .btn-history:hover:not(:disabled){background:var(--surface2);color:var(--text)}
  .btn-token{background:var(--accent);color:#fff;border:none;padding:9px 18px;border-radius:var(--radius-sm);font-size:13px;font-weight:500;cursor:pointer;font-family:var(--han)}
  .btn-token:hover{background:#4a8ce8}

  /* ===== Config grid: key(label) | value ===== */
  .cfg-grid{display:grid;grid-template-columns:minmax(180px,28%) 1fr;gap:0;border-top:1px solid var(--border-soft)}
  .cfg-section-head{
    grid-column:1/-1;font-size:12px;font-weight:600;color:var(--accent);
    padding:16px 0 8px;letter-spacing:.08em;text-transform:uppercase;
    border-bottom:1px solid var(--border-soft);margin-top:8px;
    display:flex;align-items:center;gap:8px;
  }
  .cfg-section-head::before{content:"";width:3px;height:14px;background:var(--accent);border-radius:2px}
  .cfg-section-head:first-child{margin-top:0}
  .cfg-key{
    padding:12px 14px 12px 0;border-bottom:1px solid var(--border-soft);
    display:flex;flex-direction:column;gap:2px;
  }
  .cfg-key .k-label{font-size:13.5px;font-weight:500;color:var(--text);line-height:1.4}
  .cfg-key .k-code{font-family:var(--mono);font-size:10.5px;color:var(--text-3);letter-spacing:.02em;word-break:break-all}
  .cfg-val{padding:10px 0 10px 14px;border-bottom:1px solid var(--border-soft);display:flex;align-items:center;gap:10px}
  .cfg-val .cfg-input{flex:1;min-width:0}

  /* ===== Inputs ===== */
  .cfg-input{
    width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);
    font-family:var(--mono);font-size:13px;padding:8px 11px;border-radius:var(--radius-sm);
    outline:none;transition:border-color .15s,box-shadow .15s;
  }
  .cfg-input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
  .cfg-input.changed{border-color:var(--warn);background:var(--warn-soft)}
  .cfg-input[type=checkbox]{width:20px;height:20px;accent-color:var(--accent);flex:0 0 auto;cursor:pointer}
  .cfg-input:disabled{opacity:.5;cursor:not-allowed}
  .cfg-input:disabled[type=checkbox]{cursor:not-allowed}
  textarea.cfg-input{font-size:12.5px;line-height:1.5;resize:vertical}
  .cfg-row-dirty{display:inline-block;width:8px;height:8px;background:var(--warn);border-radius:50%;flex-shrink:0;opacity:0;transition:opacity .15s}
  .cfg-row-dirty.show{opacity:1;box-shadow:0 0 6px var(--warn)}

  /* ===== History ===== */
  details{margin-top:6px}
  details summary{
    cursor:pointer;font-size:13px;font-weight:500;color:var(--text-2);padding:10px 0;
    list-style:none;display:flex;align-items:center;gap:8px;user-select:none;
  }
  details summary::-webkit-details-marker{display:none}
  details summary::before{content:"\25B6";font-size:9px;color:var(--text-3);transition:transform .15s}
  details[open] summary::before{transform:rotate(90deg)}
  details summary:hover{color:var(--text)}
  .history-panel{max-height:340px;overflow:auto;margin-top:8px;border:1px solid var(--border-soft);border-radius:var(--radius-sm)}
  .history-panel table{width:100%;border-collapse:collapse;font-size:12.5px}
  .history-panel th{
    text-align:left;color:var(--text-3);font-weight:600;font-size:11px;
    padding:9px 12px;border-bottom:1px solid var(--border);background:var(--surface2);
    position:sticky;top:0;
  }
  .history-panel td{padding:8px 12px;border-bottom:1px solid var(--border-soft);vertical-align:top}
  .history-panel tr:last-child td{border-bottom:none}
  .history-panel .ts{color:var(--text-3);white-space:nowrap;font-family:var(--mono);font-size:11.5px}
  .history-panel .ev-update{color:var(--success)}
  .history-panel .ev-rollback{color:var(--danger)}

  /* ===== Toast ===== */
  #toast-container{position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:10px}
  .toast{
    padding:12px 18px;font-size:13px;border-radius:var(--radius-sm);max-width:420px;
    word-break:break-word;animation:slidein .22s ease;box-shadow:0 8px 24px rgba(0,0,0,.35);
    border:1px solid;
  }
  .toast.ok{background:var(--success-soft);color:var(--success);border-color:rgba(76,175,130,.3)}
  .toast.err{background:var(--danger-soft);color:var(--danger);border-color:rgba(212,107,107,.3)}
  .toast.info{background:var(--accent-soft);color:var(--accent);border-color:rgba(91,157,249,.3)}
  @keyframes slidein{from{transform:translateY(16px);opacity:0}to{transform:translateY(0);opacity:1}}

  /* ===== Footer ===== */
  footer{text-align:center;font-size:12px;color:var(--text-3);margin-top:28px;font-family:var(--mono)}

  /* ===== Responsive: stack to single column on narrow screens ===== */
  @media (max-width:680px){
    .wrap{padding:16px 14px 40px}
    .stat-strip{grid-template-columns:repeat(2,1fr);gap:10px}
    .stat:nth-child(2n){transform:none}
    .stat-value{font-size:17px}
    .cfg-grid{grid-template-columns:1fr}
    .cfg-key{padding:12px 0 4px;border-bottom:none}
    .cfg-val{padding:0 0 12px 0}
    .toolbar{flex-direction:column;align-items:stretch}
    .toolbar-actions{justify-content:flex-start}
    .btn{flex:1;text-align:center}
    .token-bar input{min-width:0}
    nav{overflow-x:auto}
    .nav-link{white-space:nowrap}
    .cfg-card-body{padding:6px 12px 12px}
  }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="brand">
      <div class="brand-mark">H</div>
      <div>
        <div class="brand-title">Hermes Trader</div>
        <div class="brand-sub">configuration console</div>
      </div>
    </div>
  </header>

  <nav id="hermes-nav">
    <a href="/" data-nav="/" class="nav-link">监控面板</a>
    <a href="/config" data-nav="/config" class="nav-link">配置中心</a>
    <a href="/operator" data-nav="/operator" class="nav-link">运维台</a>
  </nav>

  <!-- 错落有致的概览统计栏 -->
  <div class="stat-strip">
    <div class="stat" id="stat-mode">
      <div class="stat-label">运行模式</div>
      <div class="stat-value" id="ov-mode">—</div>
      <div class="stat-sub" id="ov-mode-sub">实时交易开关</div>
    </div>
    <div class="stat s-ok" id="stat-crypto">
      <div class="stat-label">加密货币</div>
      <div class="stat-value" id="ov-crypto">—</div>
      <div class="stat-sub" id="ov-crypto-sub">crypto 扫描</div>
    </div>
    <div class="stat" id="stat-gates">
      <div class="stat-label">风险闸门</div>
      <div class="stat-value" id="ov-gates">—</div>
      <div class="stat-sub" id="ov-gates-sub">已启用的闸门数</div>
    </div>
    <div class="stat s-warn" id="stat-concurrent">
      <div class="stat-label">并发 / 杠杆</div>
      <div class="stat-value" id="ov-concurrent">—</div>
      <div class="stat-sub" id="ov-concurrent-sub">最大持仓 · 杠杆倍数</div>
    </div>
  </div>

  <!-- Token bar -->
  <section class="card">
    <div class="token-bar">
      <label for="token-input">运维令牌</label>
      <input id="token-input" type="password" placeholder="粘贴 HERMES_OPERATOR_TOKEN 以解锁编辑…" autocomplete="off" />
      <button id="token-save" class="btn-token">设定</button>
      <span id="auth-badge" class="auth-badge off">只读</span>
    </div>
  </section>

  <section class="card">
    <div class="toolbar">
      <span class="toolbar-meta">.agent-config.json · 每 5 秒热重载</span>
      <div class="toolbar-actions">
        <span id="cfg-mode-pill" class="cfg-mode OFF">—</span>
        <button id="btn-save" class="btn btn-save" disabled>保存更改</button>
        <button id="btn-reset" class="btn btn-reset" disabled>撤销</button>
        <button id="btn-rollback" class="btn btn-rollback" disabled>回滚备份</button>
        <button id="btn-history" class="btn btn-history">变更历史</button>
      </div>
    </div>

    <div id="cfg-grid" class="cfg-grid">
      <div class="cfg-section-head">加载中…</div>
    </div>

    <details id="history-details">
      <summary>变更历史记录</summary>
      <div id="history-panel" class="history-panel">
        <table><thead><tr><th>时间</th><th>事件</th><th>详情</th></tr></thead>
        <tbody id="history-body"><tr><td colspan="3" style="color:var(--text-3)">加载中…</td></tr></tbody></table>
      </div>
    </details>
  </section>

  <div id="toast-container"></div>

  <footer>Hermes Trader · 单钱包运行 · 不构成投资建议</footer>
</div>

<script>
// ===== Section grouping (Chinese label + config keys) =====
const SECTIONS = [
  { label: '模式与仓位', keys: ['mode','equity_fraction_per_trade','leverage','max_concurrent','max_trade_notional_usd','max_total_notional_pct'] },
  { label: '安全控制',   keys: ['max_daily_loss_usd','daily_giveback_halt_pct','daily_giveback_min_peak_usd','cooldown_min','loss_cooldown_min','min_ai_confidence','counter_regime_min_conf','chop_min_conf','chop_min_score','max_crypto_long_correlated'] },
  { label: '流动性',     keys: ['min_market_volume_usd','min_hip3_volume_usd','min_short_volume_usd','max_atr_pct','max_spread_pct'] },
  { label: '筛选器',     keys: ['coin_allowlist','coin_blocklist'] },
  { label: '市场',       keys: ['enable_crypto','enable_hip3','block_counter_trend_bypass'] },
  { label: '闸门',       keys: ['runner_entry_gate','debate_gate','signal_enforcement'] },
  { label: '仓位与退出', keys: ['atr_risk_sizing','dsl_exit','plan_b','sl_atr_mult','min_ai_close_hold_min'] },
];
const SECTION_KEYS = new Set(SECTIONS.flatMap(s => s.keys));

// Chinese display labels for each config key. English key code shown as smaller auxiliary text.
const KEY_LABELS = {
  mode: '运行模式',
  equity_fraction_per_trade: '单笔权益占比',
  leverage: '杠杆倍数',
  max_concurrent: '最大并发持仓',
  max_trade_notional_usd: '单笔最大名义金额 (USD)',
  max_total_notional_pct: '总名义金额上限占比',
  max_daily_loss_usd: '单日最大亏损 (USD)',
  daily_giveback_halt_pct: '日内回撤熔断比例',
  daily_giveback_min_peak_usd: '回撤熔断最低峰值 (USD)',
  cooldown_min: '全局冷静期 (分钟)',
  loss_cooldown_min: '亏损后冷静期 (分钟)',
  min_ai_confidence: 'AI 最低置信度',
  counter_regime_min_conf: '逆势交易最低置信度',
  chop_min_conf: '震荡市最低置信度',
  chop_min_score: '震荡市最低评分',
  max_crypto_long_correlated: '加密多头最大相关持仓',
  min_market_volume_usd: '最低市场成交量 (USD)',
  min_hip3_volume_usd: 'HIP3 最低成交量 (USD)',
  min_short_volume_usd: '做空最低成交量 (USD)',
  max_atr_pct: '最大 ATR 百分比',
  max_spread_pct: '最大价差百分比',
  coin_allowlist: '币种白名单',
  coin_blocklist: '币种黑名单',
  enable_crypto: '启用加密货币',
  enable_hip3: '启用 HIP3',
  block_counter_trend_bypass: '阻止逆势绕过',
  runner_entry_gate: '入场闸门',
  debate_gate: '辩论闸门',
  signal_enforcement: '信号强制执行',
  atr_risk_sizing: 'ATR 风险仓位',
  dsl_exit: 'DSL 退出',
  plan_b: 'B 计划',
  sl_atr_mult: '止损 ATR 倍数',
  min_ai_close_hold_min: 'AI 平仓最短持有 (分钟)',
};

// 嵌套对象的子字段中文示意。键为顶层配置键，值为 { 子字段: 中文名 } 映射。
// 在对应 JSON 编辑区上方以可折叠"字段说明"展示，避免在无 schema 的情况下盲改。
const NESTED_LABELS = {
  dsl_exit: {
    max_loss_pct: '单笔最大亏损百分比', max_loss_roe_pct: '单笔最大 ROE 亏损百分比',
    protect_pct: '盈利保护百分比', retrace_threshold: '回撤阈值（0-1）',
    hard_timeout_minutes: '硬性超时（分钟）', breakeven_trigger_pct: '保本触发百分比',
    breakeven_lock_pct: '保本锁定百分比', stale_flat_timeout_minutes: '停滞平仓超时（分钟）',
    atr_stop: 'ATR 动态止损', phase2_tiers: '二阶段分层止盈', regime_aware: '市况自适应',
    consecutive_breaches_required: '连续触发所需次数', noise_band: '噪音带过滤',
  },
  runner_entry_gate: {
    enabled: '启用闸门', allow_shorts: '允许做空', bypass_sidestep_overrides: '绕过回避覆盖',
    min_confidence: '最低置信度（做多）', min_composite: '最低综合评分（做多）',
    min_hip3_composite: 'HIP3 最低综合评分', min_short_confidence: '做空最低置信度',
    min_short_composite: '做空最低综合评分', mover_min_confidence: '异动最低置信度',
    mover_min_composite: '异动最低综合评分', pullback_long: '回调做多参数',
  },
  plan_b: {
    enabled: '启用 B 计划', rsi_low: 'RSI 低位阈值', rsi_high: 'RSI 高位阈值', size_mult: '仓位乘数',
  },
  atr_risk_sizing: {
    enabled: '启用 ATR 仓位', risk_per_trade_pct: '单笔风险百分比', sizing_basis: '仓位计算基准',
  },
  regime_classifier: {
    fast_ema: '快速 EMA 周期', slow_ema: '慢速 EMA 周期',
    slope_threshold: '斜率阈值', chop_adx_max: '震荡 ADX 上限',
  },
  debate_gate: {
    enabled: '启用辩论闸门', min_agreement: '最低同意比例', min_agree_count: '最低同意人数',
  },
  signal_enforcement: {
    enabled: '启用信号强制执行', veto: '否决生效', boost: '加权生效',
    gex_veto: 'GEX 否决生效', boost_bar_delta: '加权 K 线 Delta 阈值',
    whale_window_min: '巨鲸观察窗口（分钟）', whale_veto_min_usd: '巨鲸否决阈值（USD）',
    whale_boost_min_usd: '巨鲸加权阈值（USD）',
  },
  momentum_continuation: {
    enabled: '启用动量延续', log_near_miss: '记录擦肩而过', min_trend_pct: '最低趋势百分比',
    max_pullback_pct: '最大回调百分比', weight: '信号权重',
  },
  candlestick_patterns: {
    enabled: '启用 K 线形态', wick_body_ratio: '影线/实体比阈值',
    context_lookback: '上下文回看 K 线数', context_pct: '上下文幅度百分比',
  },
  capital_rotation: {
    enabled: '启用资金轮动', shadow_mode: '影子模式（不实盘）',
    min_candidate_composite: '候选最低综合评分', min_hold_minutes: '最短持有（分钟）',
    protect_winner_roe_pct: '盈利保护 ROE 百分比',
  },
  gex_signal: {
    enabled: '启用 GEX 信号', shadow_mode: '影子模式', caution_near_wall_pct: '临近墙警戒百分比',
  },
  shadow_signals: {
    enabled: '启用影子信号', gex: 'GEX 影子', short_volume: '做空量影子',
    crypto_whale: '加密巨鲸影子', news: '新闻影子', whale_window_min: '巨鲸窗口（分钟）',
  },
  momentum_reentry: {
    enabled: '启用动量再入', reclaim_pct: '收复百分比', min_composite: '最低综合评分',
  },
  runner_mover_surface: {
    enabled: '启用异动扫描面', min_crypto_24h_pct: '加密 24h 最低涨幅',
    min_hip3_24h_pct: 'HIP3 24h 最低涨幅', min_volume_usd: '最低成交量（USD）',
  },
};

// 顶层缺失键的中文补充（这些键存在于实时配置但未列入分组）。
const EXTRA_KEY_LABELS = {
  tp_scale_fraction: '止盈分批比例', crowded_with_min_conf: '拥挤交易最低置信度',
  min_available_margin_pct: '最低可用保证金比例', research_cooldown_min: '研究冷静期（分钟）',
  held_research_interval_min: '持仓研究间隔（分钟）', hip3_dex_allowlist: 'HIP3 DEX 白名单',
  hip3_dex_blocklist: 'HIP3 DEX 黑名单', force_execute_composite: '强制执行综合评分',
  composite_force_execute: '综合分强制执行', ta_sidestep_force_execute: '技术回避强制执行',
  ta_sidestep_min_slow_burn_count: '技术回避最低慢燃次数',
  force_execute_slow_burn_count: '强制执行慢燃次数', conviction_sizing: '信念仓位',
  whale_regime_bypass: '巨鲸市况绕过', whale_force_execute: '巨鲸强制执行',
  whale_size_multiplier: '巨鲸仓位乘数', trend_surface_enabled: '启用趋势扫描面',
  min_trend_score: '最低趋势评分', against_funding_min_conf: '逆资金费最低置信度',
  against_funding_min_score: '逆资金费最低评分', strong_trend_threshold: '强趋势阈值',
  trend_threshold: '趋势阈值', neutral_threshold: '中性阈值',
  spread_gate_fail_open: '价差闸门失败放行',
  _comment: '配置注释', override_requires_ai: '覆盖需 AI 确认',
  research_rescore_delta: '研究重评差值', momentum_continuation: '动量延续',
  candlestick_patterns: 'K 线形态', whale_scan_bypass: '巨鲸扫描绕过',
  capital_rotation: '资金轮动', gex_signal: 'GEX 信号', shadow_signals: '影子信号',
  momentum_reentry: '动量再入', runner_mover_surface: '异动扫描面',
};
Object.assign(KEY_LABELS, EXTRA_KEY_LABELS);

let liveConfig = {};
let schema = {};
let pendingChanges = {};
let operatorToken = localStorage.getItem('hermes-op-token') || '';

function toast(msg, kind) {
  kind = kind || 'ok';
  var c = document.getElementById('toast-container');
  var d = document.createElement('div');
  d.className = 'toast ' + kind;
  d.textContent = msg;
  c.appendChild(d);
  setTimeout(function(){ d.style.opacity = '0'; d.style.transition = 'opacity .3s'; setTimeout(function(){ d.remove(); }, 300); }, 4000);
}

function authHeaders() {
  return operatorToken ? { 'Authorization': 'Bearer ' + operatorToken } : {};
}

function updateAuthUI() {
  var badge = document.getElementById('auth-badge');
  var inputs = document.querySelectorAll('.cfg-input');
  if (operatorToken) {
    badge.textContent = '可编辑';
    badge.className = 'auth-badge on';
    inputs.forEach(function(el){ el.disabled = false; });
    document.getElementById('btn-rollback').disabled = false;
  } else {
    badge.textContent = '只读';
    badge.className = 'auth-badge off';
    inputs.forEach(function(el){ el.disabled = true; });
    document.getElementById('btn-save').disabled = true;
    document.getElementById('btn-reset').disabled = true;
    document.getElementById('btn-rollback').disabled = true;
    pendingChanges = {};
  }
}

function renderInput(key, val) {
  var t = (schema[key] && schema[key].type) || 'any';
  var wrap = document.createElement('div');
  wrap.className = 'cfg-val';

  // 嵌套对象：在 JSON 编辑框上方插入可折叠的中文字段说明。
  var nested = NESTED_LABELS[key];
  if (nested && (t === 'object' || t === 'any' || val !== null && typeof val === 'object' && !Array.isArray(val))) {
    var det = document.createElement('details');
    det.className = 'nested-hint';
    var sum = document.createElement('summary');
    sum.textContent = '字段说明（' + Object.keys(nested).length + ' 项）';
    var map = document.createElement('div');
    map.className = 'field-map';
    Object.keys(nested).forEach(function(sk) {
      var row = document.createElement('div');
      row.className = 'fm';
      var code = document.createElement('code');
      code.textContent = sk;
      var txt = document.createElement('span');
      txt.textContent = nested[sk];
      row.appendChild(code);
      row.appendChild(txt);
      map.appendChild(row);
    });
    det.appendChild(sum);
    det.appendChild(map);
    wrap.appendChild(det);
  }

  var input;
  if (t === 'bool') {
    input = document.createElement('input');
    input.type = 'checkbox';
    input.className = 'cfg-input';
    input.checked = !!val;
    input.addEventListener('change', function(){ onChange(key, input.checked); });
  } else if (t === 'int') {
    input = document.createElement('input');
    input.type = 'number';
    input.step = '1';
    input.className = 'cfg-input';
    input.value = val !== null && val !== undefined ? val : '';
    input.addEventListener('input', function(){ onChange(key, parseInt(input.value, 10)); });
  } else if (t === 'float') {
    input = document.createElement('input');
    input.type = 'number';
    input.step = 'any';
    input.className = 'cfg-input';
    input.value = val !== null && val !== undefined ? val : '';
    input.addEventListener('input', function(){ onChange(key, parseFloat(input.value)); });
  } else if (t === 'str') {
    input = document.createElement('input');
    input.type = 'text';
    input.className = 'cfg-input';
    input.value = val !== null && val !== undefined ? val : '';
    input.addEventListener('input', function(){ onChange(key, input.value); });
  } else if (t === 'list' || t === 'object' || t === 'any') {
    input = document.createElement('textarea');
    input.className = 'cfg-input';
    input.rows = t === 'object' ? 8 : 2;
    input.value = val !== null && val !== undefined ? JSON.stringify(val, null, 2) : '';
    input.addEventListener('input', function(){
      try {
        var parsed = JSON.parse(input.value);
        input.style.borderColor = '';
        onChange(key, parsed);
      } catch(e) {
        input.style.borderColor = 'var(--danger)';
        delete pendingChanges[key];
        updateActionButtons();
      }
    });
  }

  if (!operatorToken) input.disabled = true;

  var dot = document.createElement('span');
  dot.className = 'cfg-row-dirty';
  dot.id = 'dirty-' + key;
  dot.title = '未保存的更改';

  wrap.appendChild(input);
  wrap.appendChild(dot);
  return wrap;
}

function onChange(key, newVal) {
  var oldVal = liveConfig[key];
  if (JSON.stringify(newVal) === JSON.stringify(oldVal)) {
    delete pendingChanges[key];
    var el = document.querySelector('[data-key="' + key + '"] .cfg-input');
    if (el) el.classList.remove('changed');
    var d = document.getElementById('dirty-' + key);
    if (d) d.classList.remove('show');
  } else {
    pendingChanges[key] = newVal;
    var el2 = document.querySelector('[data-key="' + key + '"] .cfg-input');
    if (el2) el2.classList.add('changed');
    var d2 = document.getElementById('dirty-' + key);
    if (d2) d2.classList.add('show');
  }
  updateActionButtons();
}

function updateActionButtons() {
  var has = Object.keys(pendingChanges).length > 0;
  document.getElementById('btn-save').disabled = !has || !operatorToken;
  document.getElementById('btn-reset').disabled = !has;
}

function renderOverview(cfg) {
  var mode = cfg.mode || 'OFF';
  var ovMode = document.getElementById('ov-mode');
  if (ovMode) {
    ovMode.textContent = mode;
    ovMode.style.color = mode === 'LIVE' ? 'var(--success)' : 'var(--warn)';
  }
  var ovCrypto = document.getElementById('ov-crypto');
  if (ovCrypto) ovCrypto.textContent = cfg.enable_crypto ? '已启用' : '已停用';
  var gateCount = ['runner_entry_gate','debate_gate','signal_enforcement']
    .filter(function(g){ return cfg[g] && cfg[g].enabled; }).length;
  var ovGates = document.getElementById('ov-gates');
  if (ovGates) ovGates.textContent = gateCount + ' / 3';
  var ovConc = document.getElementById('ov-concurrent');
  if (ovConc) ovConc.textContent = (cfg.max_concurrent ?? '—') + ' · ' + (cfg.leverage ?? '—') + 'x';
}

function renderConfig(cfg) {
  liveConfig = cfg;
  renderOverview(cfg);
  var grid = document.getElementById('cfg-grid');
  grid.innerHTML = '';

  var mode = cfg.mode || 'OFF';
  var pill = document.getElementById('cfg-mode-pill');
  pill.textContent = '\u25c6 ' + mode;
  pill.className = 'cfg-mode ' + (mode === 'LIVE' ? 'LIVE' : 'OFF');

  var renderSection = function(label, keys) {
    var present = keys.filter(function(k){ return k in cfg; });
    if (!present.length) return;
    // 可折叠分区卡片（错落有致：偶数卡片更深阴影）
    var card = document.createElement('div');
    card.className = 'cfg-card';
    var head = document.createElement('div');
    head.className = 'cfg-card-head';
    head.innerHTML = '<span class="chev">\u25BC</span><span class="cfg-card-title">' +
      label + '</span><span class="cfg-card-count">' + present.length + ' 项</span>';
    var body = document.createElement('div');
    body.className = 'cfg-card-body';
    var inner = document.createElement('div');
    inner.className = 'cfg-grid';
    head.addEventListener('click', function(){ card.classList.toggle('collapsed'); });
    present.forEach(function(k) {
      var keyEl = document.createElement('div');
      keyEl.className = 'cfg-key';
      var labelEl = document.createElement('span');
      labelEl.className = 'k-label';
      labelEl.textContent = KEY_LABELS[k] || k;
      var codeEl = document.createElement('span');
      codeEl.className = 'k-code';
      codeEl.textContent = k;
      keyEl.appendChild(labelEl);
      keyEl.appendChild(codeEl);
      var valWrap = renderInput(k, cfg[k]);
      valWrap.dataset.key = k;
      inner.appendChild(keyEl);
      inner.appendChild(valWrap);
    });
    body.appendChild(inner);
    card.appendChild(head);
    card.appendChild(body);
    grid.appendChild(card);
  };
  SECTIONS.forEach(function(s){ renderSection(s.label, s.keys); });
  var otherKeys = Object.keys(cfg).filter(function(k){ return !SECTION_KEYS.has(k); });
  if (otherKeys.length) renderSection('其他设置', otherKeys);
}

async function loadSchema() {
  try {
    var r = await fetch('/api/dashboard/config/schema');
    if (r.ok) schema = await r.json();
  } catch(e) {}
}

async function loadConfig() {
  try {
    var r = await fetch('/api/dashboard/config');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var cfg = await r.json();
    var pending = Object.assign({}, pendingChanges);
    renderConfig(cfg);
    // Re-apply unsaved edits so the 5s hot-refresh doesn't wipe user input.
    Object.keys(pending).forEach(function(k) {
      var el = document.querySelector('[data-key="' + k + '"] .cfg-input');
      if (!el) return;
      var t = (schema[k] && schema[k].type) || 'any';
      if (t === 'bool') {
        el.checked = !!pending[k];
      } else if (t === 'list' || t === 'object' || t === 'any') {
        el.value = JSON.stringify(pending[k], null, 2);
      } else {
        el.value = pending[k];
      }
      el.classList.add('changed');
      var d = document.getElementById('dirty-' + k);
      if (d) d.classList.add('show');
    });
    pendingChanges = pending;
    updateActionButtons();
  } catch (e) {
    document.getElementById('cfg-grid').innerHTML =
      '<div class="cfg-section-head">加载失败: ' + (e.message || e) + '</div>';
  }
}

async function saveChanges() {
  if (!Object.keys(pendingChanges).length) return;
  var btn = document.getElementById('btn-save');
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    var r = await fetch('/api/dashboard/config', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      body: JSON.stringify({ updates: pendingChanges }),
    });
    var data = await r.json().catch(function(){ return {}; });
    if (!r.ok) {
      var msg = (data && data.detail) ? data.detail : ('HTTP ' + r.status);
      if (typeof msg === 'string' && msg.indexOf('{') === 0) {
        try { var parsed = JSON.parse(msg); msg = (parsed.errors || []).join('; '); } catch(e){}
      }
      toast('保存失败: ' + msg, 'err');
    } else {
      toast('已保存 ' + Object.keys(pendingChanges).length + ' 项配置', 'ok');
      pendingChanges = {};
      await loadConfig();
      loadHistory();
    }
  } catch(e) {
    toast('保存出错: ' + e.message, 'err');
  } finally {
    btn.textContent = '保存更改';
    updateActionButtons();
  }
}

function resetChanges() {
  pendingChanges = {};
  renderConfig(liveConfig);
  updateActionButtons();
  toast('已撤销未保存的更改', 'info');
}

async function rollback() {
  if (!confirm('确定回滚到上一次备份的配置？当前设置将被覆盖。')) return;
  try {
    var r = await fetch('/api/dashboard/config/rollback', {
      method: 'POST',
      headers: authHeaders(),
    });
    if (!r.ok) {
      var d = await r.json().catch(function(){ return {}; });
      toast('回滚失败: ' + (d.detail || r.status), 'err');
    } else {
      toast('配置已回滚', 'ok');
      pendingChanges = {};
      await loadConfig();
      loadHistory();
    }
  } catch(e) {
    toast('回滚出错: ' + e.message, 'err');
  }
}

async function loadHistory() {
  if (!operatorToken) {
    document.getElementById('history-body').innerHTML =
      '<tr><td colspan="3" style="color:var(--text-3)">设定运维令牌后可查看历史记录</td></tr>';
    return;
  }
  try {
    var r = await fetch('/api/dashboard/config/history', { headers: authHeaders() });
    if (r.status === 401) {
      operatorToken = '';
      localStorage.removeItem('hermes-op-token');
      updateAuthUI();
      return;
    }
    var data = await r.json();
    var h = data.history || [];
    if (!h.length) {
      document.getElementById('history-body').innerHTML =
        '<tr><td colspan="3" style="color:var(--text-3)">暂无变更记录</td></tr>';
      return;
    }
    document.getElementById('history-body').innerHTML = h.reverse().map(function(e) {
      var ts = new Date(e.ts || Date.now()).toLocaleString();
      var cls = e.event === 'config_rollback' ? 'ev-rollback' : 'ev-update';
      var detail = '';
      if (e.event === 'config_update' && e.updates) {
        detail = Object.keys(e.updates).map(function(k) {
          return k + ' = ' + JSON.stringify(e.updates[k]);
        }).join(', ');
      } else if (e.event === 'config_rollback') {
        detail = '已从 .bak 恢复';
      }
      return '<tr><td class="ts">' + ts + '</td><td class="' + cls + '">' + e.event.replace('config_','') + '</td><td>' + detail + '</td></tr>';
    }).join('');
  } catch(e) {
    document.getElementById('history-body').innerHTML =
      '<tr><td colspan="3" style="color:var(--danger)">历史记录加载失败</td></tr>';
  }
}

// ===== Token bar events =====
document.getElementById('token-save').addEventListener('click', function() {
  var v = document.getElementById('token-input').value.trim();
  operatorToken = v;
  if (v) localStorage.setItem('hermes-op-token', v);
  else localStorage.removeItem('hermes-op-token');
  updateAuthUI();
  loadHistory();
  if (v) toast('运维令牌已设定', 'ok');
});
document.getElementById('token-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') document.getElementById('token-save').click();
});

document.getElementById('btn-save').addEventListener('click', saveChanges);
document.getElementById('btn-reset').addEventListener('click', resetChanges);
document.getElementById('btn-rollback').addEventListener('click', rollback);
document.getElementById('btn-history').addEventListener('click', function() {
  var d = document.getElementById('history-details');
  d.open = !d.open;
  if (d.open) loadHistory();
});

// ===== Init =====
(function(){
  if (operatorToken) document.getElementById('token-input').value = operatorToken;
  updateAuthUI();
  loadSchema().then(loadConfig);
  setInterval(loadConfig, 5000);

  // Nav active state + token carry across pages
  var here = window.location.pathname.replace(/\/$/, '') || '/';
  document.querySelectorAll('a[data-nav]').forEach(function(a) {
    if (a.dataset.nav === here) a.classList.add('nav-active');
    if (operatorToken) {
      var u = new URL(a.href, window.location.origin);
      u.searchParams.set('token', operatorToken);
      a.href = u.toString();
    }
  });
})();
</script>
</body>
</html>
"""


_OPERATOR_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Hermes Trader · 运维台</title>
<!--
  LAN-safe: zero external CDN/font dependencies.
  All styles are inlined so the page loads on isolated local networks.
-->
<style>
  :root {
    --bg:        #1b1e25;
    --surface:   #232730;
    --surface2:  #2a2f3a;
    --border:    #353b47;
    --border-soft:#2d323c;
    --text:      #d4d9e2;
    --text-2:    #9aa3b2;
    --text-3:    #6b7280;
    --accent:    #5b9df9;
    --accent-soft:rgba(91,157,249,.14);
    --success:   #4caf82;
    --success-soft:rgba(76,175,130,.14);
    --warn:      #d4a056;
    --warn-soft: rgba(212,160,86,.14);
    --danger:    #d46b6b;
    --danger-soft:rgba(212,107,107,.14);
    --radius:    10px;
    --radius-sm: 6px;
    --mono: ui-monospace,"SF Mono","Cascadia Code",Menlo,Consolas,"Liberation Mono",monospace;
    --han: "PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans SC","Source Han Sans SC","WenQuanYi Micro Hei",system-ui,sans-serif;
  }
  *,*::before,*::after{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{
    font-family:var(--han);background:var(--bg);color:var(--text);
    line-height:1.6;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
  }
  .wrap{max-width:1180px;margin:0 auto;padding:24px 20px 48px}

  /* ===== Header ===== */
  header{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:20px}
  .brand{display:flex;align-items:center;gap:12px}
  .brand-mark{
    width:38px;height:38px;border-radius:10px;
    background:linear-gradient(135deg,var(--accent),#7c5cff);
    display:flex;align-items:center;justify-content:center;
    font-weight:700;color:#fff;font-size:18px;flex-shrink:0;
    box-shadow:0 4px 14px rgba(91,157,249,.25);
  }
  .brand-title{font-size:18px;font-weight:600;letter-spacing:.02em}
  .brand-sub{font-size:12px;color:var(--text-3);font-family:var(--mono);letter-spacing:.04em}

  /* ===== Nav ===== */
  nav{display:flex;gap:6px;margin-bottom:22px;flex-wrap:wrap;background:var(--surface);padding:6px;border-radius:var(--radius);border:1px solid var(--border-soft)}
  .nav-link{
    padding:8px 16px;font-size:13px;font-weight:500;color:var(--text-2);
    text-decoration:none;border-radius:var(--radius-sm);transition:all .15s ease;
    font-family:var(--han);
  }
  .nav-link:hover{color:var(--text);background:var(--surface2)}
  .nav-link.nav-active{color:#fff;background:var(--accent);box-shadow:0 2px 8px rgba(91,157,249,.3)}

  /* ===== Stat strip ===== */
  .stat-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}
  .stat{
    background:var(--surface);border:1px solid var(--border-soft);
    border-radius:var(--radius);padding:14px 16px;position:relative;overflow:hidden;
  }
  .stat::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent)}
  .stat.s-warn::before{background:var(--warn)}
  .stat.s-ok::before{background:var(--success)}
  .stat.s-danger::before{background:var(--danger)}
  .stat:nth-child(2n){transform:translateY(6px)}
  .stat-label{font-size:11.5px;color:var(--text-3);font-weight:500;letter-spacing:.04em}
  .stat-value{font-size:20px;font-weight:600;margin-top:4px;font-family:var(--mono);letter-spacing:.01em}
  .stat-sub{font-size:11px;color:var(--text-3);margin-top:2px}

  /* ===== Card ===== */
  .card{background:var(--surface);border:1px solid var(--border-soft);border-radius:var(--radius);padding:20px;margin-bottom:18px}
  .card-title{
    font-size:14px;font-weight:600;color:var(--text);margin:0 0 14px;
    display:flex;align-items:center;gap:8px;
  }
  .card-title .dot{width:7px;height:7px;border-radius:50%;background:var(--success);box-shadow:0 0 8px var(--success)}
  .card-title .dot.warn{background:var(--warn);box-shadow:0 0 8px var(--warn)}
  .card-title .dot.danger{background:var(--danger);box-shadow:0 0 8px var(--danger)}
  .card-title .meta{margin-left:auto;font-size:11px;color:var(--text-3);font-family:var(--mono);font-weight:400}

  /* ===== Token bar ===== */
  .token-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .token-bar label{font-size:13px;color:var(--text-2);white-space:nowrap;font-weight:500}
  .token-bar input{
    flex:1;min-width:220px;background:var(--bg);border:1px solid var(--border);
    color:var(--text);font-family:var(--mono);font-size:13px;padding:9px 12px;
    border-radius:var(--radius-sm);outline:none;transition:border-color .15s,box-shadow .15s;
  }
  .token-bar input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
  .auth-badge{font-size:11px;padding:4px 10px;border-radius:20px;font-weight:600;letter-spacing:.03em;white-space:nowrap}
  .auth-badge.on{color:var(--success);background:var(--success-soft);border:1px solid rgba(76,175,130,.3)}
  .auth-badge.off{color:var(--text-3);background:var(--surface2);border:1px solid var(--border)}

  /* ===== Buttons ===== */
  .btn{
    font-family:var(--han);font-size:13px;font-weight:500;padding:8px 16px;
    border-radius:var(--radius-sm);border:1px solid transparent;cursor:pointer;
    transition:all .15s ease;white-space:nowrap;
  }
  .btn:disabled{opacity:.38;cursor:not-allowed}
  .btn-primary{background:var(--accent);color:#fff;border-color:var(--accent)}
  .btn-primary:hover:not(:disabled){background:#4a8ce8;border-color:#4a8ce8}
  .btn-danger{background:var(--danger);color:#fff;border-color:var(--danger)}
  .btn-danger:hover:not(:disabled){background:#c05555;border-color:#c05555}
  .btn-warn{background:transparent;color:var(--warn);border-color:rgba(212,160,86,.4)}
  .btn-warn:hover:not(:disabled){background:var(--warn-soft)}
  .btn-ghost{background:transparent;color:var(--text-2);border-color:var(--border)}
  .btn-ghost:hover:not(:disabled){background:var(--surface2);color:var(--text)}
  .btn-sm{padding:5px 12px;font-size:12px}

  /* ===== Positions table ===== */
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{
    text-align:left;color:var(--text-3);font-weight:600;font-size:11.5px;
    padding:9px 12px;border-bottom:1px solid var(--border);
    letter-spacing:.04em;text-transform:uppercase;
  }
  td{padding:10px 12px;border-bottom:1px solid var(--border-soft);vertical-align:middle}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(255,255,255,.015)}
  .mono{font-family:var(--mono)}
  .text-right{text-align:right}
  .pos-long{color:var(--success);font-weight:600}
  .pos-short{color:var(--danger);font-weight:600}
  .pnl-pos{color:var(--success)}
  .pnl-neg{color:var(--danger)}

  /* ===== Trackers pre ===== */
  pre{
    font-family:var(--mono);font-size:12px;line-height:1.6;
    background:var(--bg);border:1px solid var(--border-soft);border-radius:var(--radius-sm);
    padding:14px;overflow-x:auto;margin:0;color:var(--text-2);
  }
  .empty-state{
    text-align:center;padding:32px 16px;color:var(--text-3);font-size:13px;
  }
  .empty-state .icon{font-size:28px;display:block;margin-bottom:8px;opacity:.5}

  /* ===== Mode badge ===== */
  .mode-badge{display:inline-flex;align-items:center;gap:6px;padding:5px 14px;font-size:13px;font-weight:600;border-radius:20px;border:1px solid}
  .mode-badge.LIVE{color:var(--success);background:var(--success-soft);border-color:rgba(76,175,130,.35)}
  .mode-badge.OFF{color:var(--warn);background:var(--warn-soft);border-color:rgba(212,160,86,.35)}
  .mode-badge::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}

  /* ===== Toast ===== */
  #toast-container{position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:10px}
  .toast{
    padding:12px 18px;font-size:13px;border-radius:var(--radius-sm);max-width:420px;
    word-break:break-word;animation:slidein .22s ease;box-shadow:0 8px 24px rgba(0,0,0,.35);
    border:1px solid;font-family:var(--han);
  }
  .toast.ok{background:var(--success-soft);color:var(--success);border-color:rgba(76,175,130,.3)}
  .toast.err{background:var(--danger-soft);color:var(--danger);border-color:rgba(212,107,107,.3)}
  .toast.info{background:var(--accent-soft);color:var(--accent);border-color:rgba(91,157,249,.3)}
  @keyframes slidein{from{transform:translateY(16px);opacity:0}to{transform:translateY(0);opacity:1}}

  /* ===== Danger zone ===== */
  .danger-zone{border-color:rgba(212,107,107,.3)}
  .danger-zone .card-title{color:var(--danger)}
  .danger-zone .card-title .dot{background:var(--danger);box-shadow:0 0 8px var(--danger)}
  .btn-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}

  /* ===== Refresh indicator ===== */
  .refresh-dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--success);margin-right:6px;animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

  footer{text-align:center;font-size:12px;color:var(--text-3);margin-top:28px;font-family:var(--mono)}

  /* ===== Responsive ===== */
  @media (max-width:680px){
    .wrap{padding:16px 14px 40px}
    .stat-strip{grid-template-columns:repeat(2,1fr);gap:10px}
    .stat:nth-child(2n){transform:none}
    .stat-value{font-size:17px}
    .btn-row{flex-direction:column}
    .btn-row .btn{width:100%;text-align:center}
  }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="brand">
      <div class="brand-mark">H</div>
      <div>
        <div class="brand-title">Hermes Trader</div>
        <div class="brand-sub">operator console</div>
      </div>
    </div>
  </header>

  <nav id="hermes-nav">
    <a href="/" data-nav="/" class="nav-link">监控面板</a>
    <a href="/config" data-nav="/config" class="nav-link">配置中心</a>
    <a href="/operator" data-nav="/operator" class="nav-link">运维台</a>
  </nav>

  <!-- Stat strip -->
  <div class="stat-strip">
    <div class="stat" id="stat-mode">
      <div class="stat-label">运行模式</div>
      <div class="stat-value" id="ov-mode">—</div>
      <div class="stat-sub" id="ov-mode-sub">实时交易开关</div>
    </div>
    <div class="stat s-ok" id="stat-positions">
      <div class="stat-label">持仓数量</div>
      <div class="stat-value" id="ov-positions">—</div>
      <div class="stat-sub">当前活跃持仓</div>
    </div>
    <div class="stat" id="stat-trackers">
      <div class="stat-label">DSL 追踪器</div>
      <div class="stat-value" id="ov-trackers">—</div>
      <div class="stat-sub">内存 + 持久化</div>
    </div>
    <div class="stat s-warn" id="stat-auth">
      <div class="stat-label">认证状态</div>
      <div class="stat-value" id="ov-auth">—</div>
      <div class="stat-sub">操作员令牌</div>
    </div>
  </div>

  <!-- Token bar -->
  <section class="card">
    <div class="token-bar">
      <label for="token-input">运维令牌</label>
      <input id="token-input" type="password" placeholder="粘贴 HERMES_OPERATOR_TOKEN 以解锁操作…" autocomplete="off" />
      <button id="token-save" class="btn btn-primary">设定</button>
      <span id="auth-badge" class="auth-badge off">未认证</span>
    </div>
  </section>

  <!-- Positions -->
  <section class="card">
    <div class="card-title">
      <span class="dot"></span>
      持仓管理 — 强制平仓
      <span class="meta"><span class="refresh-dot"></span>每 10 秒自动刷新</span>
    </div>
    <div id="positions-wrap">
      <div class="empty-state"><span class="icon">◌</span>加载中…</div>
    </div>
  </section>

  <!-- DSL trackers -->
  <section class="card">
    <div class="card-title">
      <span class="dot warn"></span>
      DSL 追踪器
      <span class="meta">内存 + 持久化状态</span>
    </div>
    <div id="trackers-wrap">
      <pre id="trackers">加载中…</pre>
    </div>
  </section>

  <!-- Danger zone -->
  <section class="card danger-zone">
    <div class="card-title">
      <span class="dot"></span>
      危险操作区
    </div>
    <div class="btn-row">
      <button class="btn btn-danger" onclick="setMode('OFF')">⏸ 切换至暂停模式（停止新开仓）</button>
      <button class="btn btn-warn" onclick="setMode('LIVE')">▶ 切换至运行模式</button>
      <span id="mode-display" style="margin-left:auto"></span>
    </div>
  </section>

  <footer>
    Hermes Trader Operator Console · 令牌仅存储于浏览器 localStorage
  </footer>
</div>

<div id="toast-container"></div>

<script>
// ===== Token resolution =====
const params = new URLSearchParams(location.search);
const tokenFromUrl = params.get('token') || '';
let token = tokenFromUrl || localStorage.getItem('hermes-op-token') || '';
if (tokenFromUrl) localStorage.setItem('hermes-op-token', tokenFromUrl);

const tokenInput = document.getElementById('token-input');
const tokenSave = document.getElementById('token-save');
const authBadge = document.getElementById('auth-badge');
tokenInput.value = token;

function setAuth(on) {
  authBadge.textContent = on ? '已认证' : '未认证';
  authBadge.className = 'auth-badge ' + (on ? 'on' : 'off');
  document.getElementById('ov-auth').textContent = on ? '已认证' : '未认证';
  document.getElementById('stat-auth').className = 'stat ' + (on ? 's-ok' : 's-warn');
}
setAuth(!!token);

tokenSave.addEventListener('click', () => {
  token = tokenInput.value.trim();
  if (token) {
    localStorage.setItem('hermes-op-token', token);
    setAuth(true);
    toast('令牌已保存', 'ok');
    loadTrackers(); loadPositions();
    // Carry token to nav links
    carryTokenToNav();
  } else {
    localStorage.removeItem('hermes-op-token');
    setAuth(false);
    toast('令牌已清除', 'info');
  }
});

const auth = () => ({'X-Operator-Token': token || ''});

// ===== Toast =====
function toast(msg, kind) {
  kind = kind || 'info';
  const c = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; setTimeout(() => el.remove(), 300); }, 3500);
}

// ===== Nav: highlight active + carry token =====
function carryTokenToNav() {
  document.querySelectorAll('a[data-nav]').forEach(a => {
    if (token) {
      const u = new URL(a.href, window.location.origin);
      u.searchParams.set('token', token);
      a.href = u.toString();
    }
  });
}
(function(){
  const here = window.location.pathname.replace(/\/$/, '') || '/';
  document.querySelectorAll('a[data-nav]').forEach(a => {
    if (a.dataset.nav === here) a.classList.add('nav-active');
  });
  carryTokenToNav();
})();

// ===== Positions =====
async function loadPositions() {
  try {
    const r = await fetch('/api/dashboard/positions');
    const ps = await r.json();
    const wrap = document.getElementById('positions-wrap');
    document.getElementById('ov-positions').textContent = ps.length;

    if (!ps.length) {
      wrap.innerHTML = '<div class="empty-state"><span class="icon">◌</span>当前无持仓</div>';
      return;
    }

    let html = '<table><thead><tr>'
      + '<th>币种</th><th>方向</th><th class="text-right">数量</th>'
      + '<th class="text-right">开仓价</th><th class="text-right">未实现盈亏</th>'
      + '<th class="text-right">操作</th>'
      + '</tr></thead><tbody>';
    ps.forEach(p => {
      const sideCls = p.side === 'long' ? 'pos-long' : 'pos-short';
      const sideTxt = p.side === 'long' ? '做多' : '做空';
      const pnlCls = p.unrealized_pct >= 0 ? 'pnl-pos' : 'pnl-neg';
      const pnlSign = p.unrealized_pct >= 0 ? '+' : '';
      html += '<tr>'
        + '<td class="mono" style="font-weight:600">' + p.coin + '</td>'
        + '<td><span class="' + sideCls + '">' + sideTxt + '</span></td>'
        + '<td class="text-right mono">' + p.size.toFixed(4) + '</td>'
        + '<td class="text-right mono">' + p.entry_px.toFixed(2) + '</td>'
        + '<td class="text-right mono ' + pnlCls + '">' + pnlSign + p.unrealized_pct.toFixed(2) + '%</td>'
        + '<td class="text-right"><button class="btn btn-danger btn-sm" onclick="closeCoin(\'' + p.coin + '\')">强平</button></td>'
        + '</tr>';
    });
    html += '</tbody></table>';
    wrap.innerHTML = html;
  } catch (e) {
    document.getElementById('positions-wrap').innerHTML = '<div class="empty-state">持仓数据加载失败</div>';
  }
}

// ===== Trackers =====
async function loadTrackers() {
  if (!token) {
    document.getElementById('trackers').textContent = '未设置运维令牌，请在上方输入 HERMES_OPERATOR_TOKEN 后查看。';
    document.getElementById('trackers').style.color = 'var(--text-3)';
    document.getElementById('ov-trackers').textContent = '—';
    return;
  }
  try {
    const r = await fetch('/api/dashboard/operator/trackers', {headers: auth()});
    if (r.status === 401) {
      setAuth(false);
      document.getElementById('trackers').textContent = '令牌被服务器拒绝（401），请重新输入。';
      document.getElementById('trackers').style.color = 'var(--danger)';
      return;
    }
    if (r.status === 503) {
      document.getElementById('trackers').textContent = '后端未配置 HERMES_OPERATOR_TOKEN，运维功能已禁用。';
      document.getElementById('trackers').style.color = 'var(--warn)';
      return;
    }
    const data = await r.json();
    const el = document.getElementById('trackers');
    document.getElementById('ov-trackers').textContent = Array.isArray(data) ? data.length : 0;

    if (!Array.isArray(data) || data.length === 0) {
      el.textContent = '无活跃 DSL 追踪器 — 当前没有持仓被追踪。\n（空仓时这是正常状态。）';
      el.style.color = 'var(--text-3)';
      el.style.fontStyle = 'italic';
    } else {
      el.textContent = JSON.stringify(data, null, 2);
      el.style.color = '';
      el.style.fontStyle = '';
    }
  } catch (e) {
    document.getElementById('trackers').textContent = '追踪器数据加载失败：' + e.message;
    document.getElementById('trackers').style.color = 'var(--danger)';
  }
}

// ===== Actions =====
async function closeCoin(coin) {
  if (!confirm('确认强制平仓 ' + coin + '？此操作不可撤销。')) return;
  try {
    const r = await fetch('/api/dashboard/operator/close', {
      method: 'POST', headers: {...auth(), 'Content-Type': 'application/json'},
      body: JSON.stringify({coin})
    });
    const data = await r.json();
    if (r.ok) {
      toast(coin + ' 平仓指令已发送', 'ok');
    } else {
      toast('平仓失败：' + (data.detail || JSON.stringify(data)), 'err');
    }
    loadPositions();
  } catch (e) {
    toast('请求失败：' + e.message, 'err');
  }
}

async function setMode(mode) {
  if (mode === 'LIVE' && !confirm('确认切换至运行模式？将允许新开仓。')) return;
  if (mode === 'OFF' && !confirm('确认切换至暂停模式？将停止所有新开仓。')) return;
  try {
    const r = await fetch('/api/dashboard/operator/mode', {
      method: 'POST', headers: {...auth(), 'Content-Type': 'application/json'},
      body: JSON.stringify({mode})
    });
    const data = await r.json();
    if (r.ok) {
      toast('模式已切换为：' + (data.mode || mode), 'ok');
      updateModeDisplay(data.mode || mode);
    } else {
      toast('模式切换失败：' + (data.detail || JSON.stringify(data)), 'err');
    }
  } catch (e) {
    toast('请求失败：' + e.message, 'err');
  }
}

function updateModeDisplay(mode) {
  const el = document.getElementById('mode-display');
  const ovMode = document.getElementById('ov-mode');
  const ovSub = document.getElementById('ov-mode-sub');
  const label = mode === 'LIVE' ? '运行中' : '已暂停';
  el.innerHTML = '<span class="mode-badge ' + mode + '">' + label + '</span>';
  ovMode.textContent = label;
  ovSub.textContent = mode === 'LIVE' ? '允许新开仓' : '停止新开仓';
  document.getElementById('stat-mode').className = 'stat ' + (mode === 'LIVE' ? 's-ok' : 's-warn');
}

// ===== Init + auto-refresh =====
loadPositions();
loadTrackers();
setInterval(loadPositions, 10000);
setInterval(loadTrackers, 10000);
</script>
</body>
</html>
"""


# ── route registration ──────────────────────────────────────────────────────


def register_routes(app: FastAPI) -> None:
    """Mount dashboard + SSE + operator routes onto an existing FastAPI app."""

    # ── Vue SPA (hermes-web) static hosting ─────────────────────────────
    # Production build mounted at /web/. Built with `--base=/web/`.
    _WEB_DIST = "/app/web-dist"
    # no-store on both dashboards so a server restart isn't masked by a cached
    # HTML shell that pre-dates the new JS. The JSON endpoints below are fine
    # to cache for their poll interval.
    _NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

    if os.path.isdir(_WEB_DIST):

        class _SPAStaticFiles(StaticFiles):
            """StaticFiles that falls back to index.html for unknown paths.

            The Vue router uses history mode, so deep links like /web/positions
            or /web/closed-trades don't correspond to files on disk. The default
            StaticFiles returns 404; this subclass serves the SPA shell instead
            so client-side routing can take over.
            """

            async def get_response(self, path: str, scope):
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
        # Root now redirects to the Vue SPA (Command Center). The legacy
        # pixel-style dashboard remains available at /legacy for fallback.
        return RedirectResponse(url="/web/", status_code=302)

    @app.get("/legacy", response_class=HTMLResponse)
    async def legacy_dashboard() -> HTMLResponse:
        return HTMLResponse(content=_PUBLIC_HTML, headers=_NO_CACHE_HEADERS)

    @app.get("/operator", response_class=HTMLResponse)
    async def operator_console() -> HTMLResponse:
        # No token gate on the HTML itself — the page is a shell that calls
        # token-gated APIs. Without a valid ?token=… the AJAX calls 401 and the
        # page shows "loading…" with no data. Cheap defense, no auth library.
        return HTMLResponse(content=_OPERATOR_HTML, headers=_NO_CACHE_HEADERS)

    @app.get("/config", response_class=HTMLResponse)
    async def config_page() -> HTMLResponse:
        """Live agent-config viewer. Read-only — mutations happen via the
        Cmd+K terminal's `set <key> <value>` command."""
        return HTMLResponse(content=_CONFIG_HTML, headers=_NO_CACHE_HEADERS)

    @app.get("/api/dashboard/config")
    async def dashboard_config() -> JSONResponse:
        """Read-only JSON dump of `.agent-config.json` for the /config page.
        Hot-reloads alongside the trading loop (no caching)."""
        cfg = await asyncio.to_thread(read_agent_config)
        return JSONResponse(cfg)

    # ── config write API (operator-gated) ─────────────────────────────────

    _CONFIG_TYPES: Dict[str, type] = {
        "mode": str,
        "enable_crypto": bool, "enable_hip3": bool,
        "equity_fraction_per_trade": float, "leverage": int,
        "max_concurrent": int, "max_trade_notional_usd": float,
        "max_total_notional_pct": float, "max_daily_loss_usd": float,
        "daily_giveback_halt_pct": float, "daily_giveback_min_peak_usd": float,
        "cooldown_min": int, "min_ai_confidence": float,
        "counter_regime_min_conf": float, "max_crypto_long_correlated": int,
        "min_market_volume_usd": float, "min_hip3_volume_usd": float,
        "min_short_volume_usd": float, "chop_min_conf": float,
        "chop_min_score": float, "max_atr_pct": float, "max_spread_pct": float,
        "loss_cooldown_min": int, "min_ai_close_hold_min": int,
        "sl_atr_mult": float, "min_trend_score": float,
        "block_counter_trend_bypass": bool, "whale_regime_bypass": bool,
        "conviction_sizing": bool, "breakout_force_execute": bool,
        "spread_gate_fail_open": bool,
        "override_requires_ai": bool,
        "whale_scan_bypass": bool,
        "research_rescore_delta": float,
    }

    def _validate_config_updates(updates: Dict[str, Any]) -> List[str]:
        """Validate a partial update dict. Returns a list of error strings."""
        errors: List[str] = []
        for key, val in updates.items():
            if key not in CANONICAL_DEFAULTS:
                errors.append(f"unknown key: {key}")
                continue
            expected = _CONFIG_TYPES.get(key)
            if expected is not None:
                if expected is int:
                    if not isinstance(val, int) or isinstance(val, bool):
                        errors.append(f"{key}: expected int, got {type(val).__name__}")
                elif expected is float:
                    if not isinstance(val, (int, float)) or isinstance(val, bool):
                        errors.append(f"{key}: expected number, got {type(val).__name__}")
                elif expected is bool:
                    if not isinstance(val, bool):
                        errors.append(f"{key}: expected bool, got {type(val).__name__}")
                elif expected is str:
                    if not isinstance(val, str):
                        errors.append(f"{key}: expected string, got {type(val).__name__}")
            # List/dict values (coin_allowlist, dsl_exit, debate_gate etc.)
            # are accepted as-is if the default is also list/dict.
            elif isinstance(CANONICAL_DEFAULTS.get(key), list) and not isinstance(val, list):
                errors.append(f"{key}: expected list, got {type(val).__name__}")
            elif isinstance(CANONICAL_DEFAULTS.get(key), dict) and not isinstance(val, dict):
                errors.append(f"{key}: expected object, got {type(val).__name__}")
        # Range checks
        if "leverage" in updates and isinstance(updates["leverage"], int):
            if updates["leverage"] < 1 or updates["leverage"] > 50:
                errors.append("leverage: must be 1–50")
        if "max_concurrent" in updates and isinstance(updates["max_concurrent"], int):
            if updates["max_concurrent"] < 0:
                errors.append("max_concurrent: must be >= 0")
        if "min_ai_confidence" in updates and isinstance(updates["min_ai_confidence"], (int, float)):
            if not (0.0 <= updates["min_ai_confidence"] <= 1.0):
                errors.append("min_ai_confidence: must be 0.0–1.0")
        if "equity_fraction_per_trade" in updates and isinstance(updates["equity_fraction_per_trade"], (int, float)):
            if not (0.0 < updates["equity_fraction_per_trade"] <= 1.0):
                errors.append("equity_fraction_per_trade: must be > 0 and <= 1.0")
        return errors

    @app.post("/api/dashboard/config")
    async def dashboard_config_write(request: Request) -> JSONResponse:
        """Apply a partial config update. Operator token required.

        Body: ``{"updates": {"key": value, ...}}``.
        Validates types/ranges, backs up the current config, writes atomically,
        and appends an audit event to the session log.
        """
        _require_operator(request)
        body = await request.json()
        updates = body.get("updates")
        if not isinstance(updates, dict) or not updates:
            raise HTTPException(400, "updates must be a non-empty object")

        errors = _validate_config_updates(updates)
        if errors:
            raise HTTPException(422, json.dumps({"errors": errors}))

        def _apply() -> Dict[str, Any]:
            cfg = read_agent_config()
            old_snapshot = {k: cfg.get(k) for k in updates}
            cfg.update(updates)
            write_agent_config(cfg, backup=True)
            return {"old": old_snapshot, "new": {k: cfg[k] for k in updates}}

        result = await asyncio.to_thread(_apply)
        session_log.append({
            "event": "config_update",
            "ts": int(time.time() * 1000),
            "updates": {k: v for k, v in updates.items()},
            "old": result["old"],
        })
        logger.info("config update via dashboard: %s", list(updates.keys()))
        return JSONResponse({"ok": True, "applied": result["new"]})

    @app.get("/api/dashboard/config/backup")
    async def dashboard_config_backup(request: Request) -> JSONResponse:
        """Return the last backed-up config (before the last write)."""
        _require_operator(request)
        bak = await asyncio.to_thread(backup_config)
        if bak is None:
            return JSONResponse({"available": False, "config": None})
        return JSONResponse({"available": True, "config": bak})

    @app.post("/api/dashboard/config/backup")
    async def dashboard_config_snapshot(request: Request) -> JSONResponse:
        """Create a named manual snapshot of the current config.

        Unlike the rolling ``.bak`` (overwritten on every write), manual
        snapshots are immutable, timestamped, and retained up to a cap so the
        operator can checkpoint a known-good config before risky edits.
        """
        _require_operator(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        reason = "manual"
        if isinstance(body, dict):
            r = body.get("reason")
            if isinstance(r, str) and r.strip():
                reason = r.strip()[:80]
        try:
            snap = await asyncio.to_thread(create_snapshot, reason)
        except OSError as e:
            raise HTTPException(500, f"snapshot failed: {e}")
        session_log.append({
            "event": "config_snapshot",
            "ts": int(time.time() * 1000),
            "snapshot_id": snap["id"],
            "reason": reason,
        })
        logger.info("manual config snapshot created: %s", snap["id"])
        return JSONResponse({"ok": True, "snapshot": snap})

    @app.post("/api/dashboard/config/rollback")
    async def dashboard_config_rollback(request: Request) -> JSONResponse:
        """Restore the config.

        Body is optional. ``{"id": "snap-<ts>"}`` restores a specific manual
        snapshot; with no body (or no id) it restores the rolling ``.bak``
        produced by the last write.
        """
        _require_operator(request)
        target = None
        try:
            body = await request.json()
            if isinstance(body, dict):
                target = body.get("id")
        except Exception:
            target = None

        if isinstance(target, str) and target.startswith("snap-"):
            try:
                ts = int(target[len("snap-"):])
            except ValueError:
                raise HTTPException(400, "invalid snapshot id")
            ok = await asyncio.to_thread(restore_snapshot, ts)
            if not ok:
                raise HTTPException(404, f"snapshot {target} not found")
            restored_from = target
        else:
            ok = await asyncio.to_thread(restore_backup)
            if not ok:
                raise HTTPException(409, "no backup available to restore")
            restored_from = "backup"
        session_log.append({
            "event": "config_rollback",
            "ts": int(time.time() * 1000),
            "from": restored_from,
        })
        logger.warning("config rolled back via dashboard (from=%s)", restored_from)
        cfg = await asyncio.to_thread(read_agent_config)
        return JSONResponse({"ok": True, "from": restored_from, "config": cfg})

    @app.get("/api/dashboard/config/history")
    async def dashboard_config_history(request: Request) -> JSONResponse:
        """Recent config activity plus available manual snapshots.

        Returns ``{"history": [...], "snapshots": [...]}``. History items are
        config_update / config_rollback / config_snapshot events (newest
        last, capped at 30). Snapshots are manual recovery points (newest
        first) and can be passed as ``id`` to the rollback endpoint.
        """
        _require_operator(request)
        events = await asyncio.to_thread(session_log.tail, 200)
        history = [
            e for e in (events or [])
            if e.get("event") in (
                "config_update", "config_rollback", "config_snapshot",
            )
        ][-30:]
        snapshots = await asyncio.to_thread(list_snapshots)
        return JSONResponse({"history": history, "snapshots": snapshots})

    @app.get("/api/dashboard/config/schema")
    async def dashboard_config_schema() -> JSONResponse:
        """Return key metadata (type + default) for building the edit form."""
        schema: Dict[str, Any] = {}
        for key, default in CANONICAL_DEFAULTS.items():
            t = _CONFIG_TYPES.get(key)
            if t is int:
                type_name = "int"
            elif t is float:
                type_name = "float"
            elif t is bool:
                type_name = "bool"
            elif t is str:
                type_name = "str"
            elif isinstance(default, list):
                type_name = "list"
            elif isinstance(default, dict):
                type_name = "object"
            else:
                type_name = "any"
            schema[key] = {"type": type_name, "default": default}
        return JSONResponse(schema)

    @app.get("/api/dashboard/summary")
    async def dashboard_summary() -> JSONResponse:
        # Payload reads + parses the full session log on every cache miss
        # (~150ms). Run it in a worker thread so a long read doesn't block
        # the asyncio event loop and starve other routes / SSE clients.
        payload = await asyncio.to_thread(_ttl_cached, "summary", 2.0, _summary_payload)
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
            _ttl_cached, f"equity-curve:{range_s}", 30.0,
            lambda: _equity_curve_payload(range_s),
        )
        return JSONResponse(payload)

    @app.get("/api/dashboard/closed-trades")
    async def dashboard_closed_trades(limit: int = Query(20, ge=1, le=200)) -> JSONResponse:
        payload = await asyncio.to_thread(
            _ttl_cached, f"closed-trades:{limit}", 10.0,
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

    # ── operator (token-gated) ──

    @app.get("/api/dashboard/operator/config")
    async def operator_config(request: Request) -> JSONResponse:
        _require_operator(request)
        return JSONResponse(read_agent_config())

    @app.get("/api/dashboard/operator/trackers")
    async def operator_trackers(request: Request) -> JSONResponse:
        _require_operator(request)
        dsl_exit.load_state(force=True)
        out = []
        for key, t in dsl_exit._active_positions.items():
            out.append({
                "key": key, "coin": t.coin, "side": t.side,
                "entry_px": t.entry_px, "peak_px": t.peak_px,
                "floor_px": t._last_floor, "entry_time": t.entry_time,
                "consecutive_breaches": t.consecutive_breaches,
            })
        return JSONResponse(out)

    @app.post("/api/dashboard/operator/close")
    async def operator_close(request: Request) -> JSONResponse:
        _require_operator(request)
        body = await request.json()
        coin = (body.get("coin") or "").upper()
        if not coin:
            raise HTTPException(400, "coin required")
        from hermes_trader.agents.executor import close_position_market
        return JSONResponse(close_position_market(coin))

    @app.post("/api/dashboard/operator/mode")
    async def operator_mode(request: Request) -> JSONResponse:
        _require_operator(request)
        from hermes_trader.agents.config_store import write_agent_config
        body = await request.json()
        mode = (body.get("mode") or "").upper()
        if mode not in {"OFF", "LIVE"}:
            raise HTTPException(400, "mode must be OFF or LIVE")
        cfg = read_agent_config()
        cfg["mode"] = mode
        write_agent_config(cfg)
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
        _require_operator(request)
        body = await request.json()
        cmd = (body.get("command") or "").strip()
        if not cmd:
            return JSONResponse({"response": "", "kind": "noop"})
        parts = cmd.split()
        verb = parts[0].lower()

        # ── built-in commands ─────────────────────────────────────────────
        if verb in ("help", "?"):
            return JSONResponse({"response": (
                "commands:\n"
                "  status                — equity, daily PnL, open, tick, scan triggers\n"
                "  positions             — live positions w/ uPnL (winners + losers grouped)\n"
                "  trades [n]            — last n real fills from memory (default 10)\n"
                "  config                — dump current .agent-config.json\n"
                "  dump                  — full state (config + positions + last events)\n"
                "  regime                — cached regime per proxy\n"
                "  pause / resume        — flip mode OFF/LIVE\n"
                "  close <coin>          — market-close a single position\n"
                "  close all             — market-close every open position\n"
                "  close losing          — market-close every position with uPnL < 0\n"
                "  close winning         — market-close every position with uPnL > 0\n"
                "  set <key> <value>     — update .agent-config.json (int/float/bool/str inferred)\n"
                "  kill                  — pause trading then close all (panic button)\n"
                "  help                  — this list. anything else → ask the chat model"
            ), "kind": "help"})

        if verb == "status":
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

        if verb in ("pause", "resume"):
            new_mode = "OFF" if verb == "pause" else "LIVE"
            from hermes_trader.agents.config_store import write_agent_config
            cfg = read_agent_config()
            old = cfg.get("mode", "?")
            cfg["mode"] = new_mode
            write_agent_config(cfg)
            return JSONResponse({"response": f"mode {old} → {new_mode}", "kind": "action"})

        # ── close: single coin, all, losing, or winning ─────────────────
        if verb == "close" and len(parts) >= 2:
            from hermes_trader.agents.executor import close_position_market
            target = parts[1].lower()
            if target in ("all", "losing", "winning"):
                # Bulk close — iterate live positions, filter, close each.
                try:
                    user = resolve_user_address()
                    # include_hip3=True so `close all` also closes xyz:/vntl:/...
                    # positions, not just main-dex.
                    state = fetch_account_state(user, include_hip3=True) if user else {}
                    open_pos = [
                        {
                            "coin": p.get("position", {}).get("coin"),
                            "szi": float(p.get("position", {}).get("szi", "0") or 0),
                            "uPnL": float(p.get("position", {}).get("unrealizedPnl", "0") or 0),
                        }
                        for p in state.get("asset_positions", []) or []
                        if float(p.get("position", {}).get("szi", "0") or 0) != 0
                    ]
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
                for p in targets:
                    coin = p["coin"]
                    try:
                        r = close_position_market(coin)
                        ok = bool(r.get("ok") or r.get("executed"))
                        results.append(f"  {coin:<14} {('✓' if ok else '✗')} uPnL={p['uPnL']:+.2f}")
                    except Exception as e:
                        results.append(f"  {coin:<14} ✗ {e}")
                head = f"closed {len(targets)} position(s) [{target}]:\n"
                return JSONResponse({"response": head + "\n".join(results), "kind": "action"})

            # Single-coin close (preserve original behavior)
            coin = parts[1] if ":" in parts[1] else parts[1].upper()
            result = close_position_market(coin)
            return JSONResponse({"response": f"close {coin}: {result}", "kind": "action"})

        # ── positions: live list grouped by winners / losers ───────────
        if verb == "positions":
            try:
                user = resolve_user_address()
                state = fetch_account_state(user, include_hip3=True) if user else {}
                rows = []
                for p in state.get("asset_positions", []) or []:
                    pos = p.get("position", {})
                    szi = float(pos.get("szi", "0") or 0)
                    if szi == 0:
                        continue
                    rows.append({
                        "coin": pos.get("coin"),
                        "side": "long" if szi > 0 else "short",
                        "size": abs(szi),
                        "entry": float(pos.get("entryPx", "0") or 0),
                        "uPnL": float(pos.get("unrealizedPnl", "0") or 0),
                    })
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

        # ── trades [n]: last n real fills from memory ──────────────────
        if verb == "trades":
            try:
                from hermes_trader.agents.memory import memory as _mem
                _mem.load()
                n = 10
                if len(parts) >= 2:
                    try: n = max(1, min(50, int(parts[1])))
                    except ValueError: pass
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

        # ── set <key> <value>: update agent config (type-inferred) ─────
        if verb == "set" and len(parts) >= 3:
            from hermes_trader.agents.config_store import write_agent_config
            key = parts[1]
            raw = " ".join(parts[2:]).strip()
            # Type coercion: int, float, bool, json, else string.
            def _coerce(s: str):
                if s.lower() in ("true", "false"):
                    return s.lower() == "true"
                if s.lower() in ("null", "none"):
                    return None
                try: return int(s)
                except ValueError: pass
                try: return float(s)
                except ValueError: pass
                if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
                    try: return json.loads(s)
                    except Exception: pass
                return s
            new_val = _coerce(raw)
            cfg = read_agent_config()
            old_val = cfg.get(key, "<unset>")
            cfg[key] = new_val
            write_agent_config(cfg)
            return JSONResponse({"response": f"config[{key}]: {old_val} → {new_val}  (type={type(new_val).__name__})",
                                  "kind": "action"})

        # ── kill: pause + close all (panic button) ─────────────────────
        if verb == "kill":
            from hermes_trader.agents.config_store import write_agent_config
            from hermes_trader.agents.executor import close_position_market
            cfg = read_agent_config()
            cfg["mode"] = "OFF"
            write_agent_config(cfg)
            try:
                user = resolve_user_address()
                state = fetch_account_state(user, include_hip3=True) if user else {}
                open_coins = [
                    p["position"]["coin"]
                    for p in state.get("asset_positions", []) or []
                    if float(p.get("position", {}).get("szi", "0") or 0) != 0
                ]
            except Exception as e:
                return JSONResponse({"response": f"mode → OFF, but position-list fetch failed: {e}", "kind": "error"})
            closed = []
            for c in open_coins:
                try:
                    r = close_position_market(c)
                    closed.append(f"  {c}: {'✓' if (r.get('ok') or r.get('executed')) else '✗'}")
                except Exception as e:
                    closed.append(f"  {c}: ✗ {e}")
            head = f"KILL · mode → OFF · closed {len(open_coins)} position(s):\n"
            return JSONResponse({"response": head + ("\n".join(closed) if closed else "  (no positions to close)"),
                                  "kind": "action"})

        # ── dump: full state snapshot (config + positions + last events) ─
        if verb == "dump":
            try:
                user = resolve_user_address()
                state = fetch_account_state(user, include_hip3=True) if user else {}
                events = session_log.tail(10) or []
                positions = [
                    {"coin": p.get("position", {}).get("coin"),
                     "szi": float(p.get("position", {}).get("szi", "0") or 0),
                     "uPnL": float(p.get("position", {}).get("unrealizedPnl", "0") or 0)}
                    for p in state.get("asset_positions", []) or []
                    if float(p.get("position", {}).get("szi", "0") or 0) != 0
                ]
                snap = {
                    "config": read_agent_config(),
                    "equity": float(state.get("equity", 0) or 0),
                    "open_positions": positions,
                    "recent_events": [{k: v for k, v in e.items() if k != "ts"} for e in events],
                }
                return JSONResponse({"response": json.dumps(snap, indent=2, default=str), "kind": "info"})
            except Exception as e:
                return JSONResponse({"response": f"dump failed: {e}", "kind": "error"})

        if verb == "regime":
            try:
                from hermes_trader.agents.market_regime import regime_snapshot
                snap = regime_snapshot()
                lines = [f"  {p}: {info.get('regime', '?')}  ({int(info.get('age_s', 0))}s old)"
                         for p, info in snap.items()]
                return JSONResponse({"response": "regime snapshot:\n" + "\n".join(lines) if lines else "no cached regimes yet",
                                      "kind": "info"})
            except Exception as e:
                return JSONResponse({"response": f"regime fetch failed: {e}", "kind": "error"})

        if verb == "config":
            cfg = read_agent_config()
            return JSONResponse({"response": json.dumps(cfg, indent=2), "kind": "info"})

        # ── LLM fallback (Nous Hermes via OpenRouter) ─────────────────────
        try:
            import httpx
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
            try:
                user = resolve_user_address()
                state = fetch_account_state(user, include_hip3=True) if user else {}
                open_pos = [
                    {
                        "coin": p.get("position", {}).get("coin"),
                        "side": "long" if float(p.get("position", {}).get("szi", "0") or 0) > 0 else "short",
                        "szi": float(p.get("position", {}).get("szi", "0") or 0),
                        "entry": float(p.get("position", {}).get("entryPx", "0") or 0),
                        "uPnL": float(p.get("position", {}).get("unrealizedPnl", "0") or 0),
                    }
                    for p in state.get("asset_positions", []) or []
                    if float(p.get("position", {}).get("szi", "0") or 0) != 0
                ]
            except Exception:
                open_pos = []

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
            async def _call():
                async with httpx.AsyncClient(timeout=20.0) as client:
                    r = await client.post(
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
                    return r.json()
            # We're inside FastAPI's event loop here, so just await directly.
            data = await _call()
            content = data["choices"][0]["message"]["content"].strip()
            return JSONResponse({"response": content, "kind": "chat", "model": chat_model})
        except Exception as e:
            return JSONResponse({"response": f"chat error: {e}", "kind": "error"})

    # ── SPA catch-all: serve index.html for any non-API GET route ───────
    # Vue Router uses history mode, so deep links like /positions must
    # return the SPA shell instead of FastAPI's default {"detail":"Not Found"}.
    # API routes (/api/, /metrics, /postmortems) are already registered above
    # and take precedence; this catches everything else.
    _spa_index = os.path.join(_WEB_DIST, "index.html") if os.path.isdir(_WEB_DIST) else ""

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_catch_all(full_path: str):
        # Never intercept API or known backend routes.
        if (full_path.startswith("api/") or full_path.startswith("metrics")
                or full_path.startswith("postmortems") or full_path.startswith("web/")):
            raise HTTPException(status_code=404)
        if _spa_index and os.path.isfile(_spa_index):
            return FileResponse(_spa_index, headers=_NO_CACHE_HEADERS)
        # SPA not built: fall back to redirect to root.
        return RedirectResponse(url="/", status_code=302)
