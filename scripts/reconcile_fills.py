#!/usr/bin/env python3
"""Daily reconciliation: exchange userFills vs in-memory trade/close records.

Detects the exact failure class behind the 2026-08-22 PURR incident — a
position closed on the exchange whose outcome never reached the local
outcome store — before it can accumulate into a silent bookkeeping gap.

For the look-back window (default: last 26h to cover a UTC day plus an
overnight margin), the script:

  1. Pulls the wallet's recent fills from Hyperliquid ``userFills``.
  2. Splits them into OPEN fills (side matches a position increase) and
     CLOSE fills (a fill that realises PnL, i.e. ``closedPnl != 0``).
  3. Compares against the local ``memory.get_all_trades()`` (by order_id)
     and ``memory.get_closes()`` (by close_oid / coin+time bucket):
       * **orphan-open**: an exchange fill opened a position but memory
         has no trade with that order_id (fill happened while the loop was
         down / order was placed outside the bot).
       * **orphan-close**: an exchange fill closed a position (closedPnl
         set) but memory has no matching close record (the PURR bug).
       * **phantom-local**: a local close record has no matching exchange
         fill in the window (stale/duplicate bookkeeping).
  4. Exits non-zero if any orphan is found, so cron can alert. Optionally
     ``--auto-backfill`` appends missing close records through the same
     ``memory.record_close`` chokepoint the live loop uses (which now also
     writes events.jsonl).

Designed to run inside the container once a day, e.g. at 00:15 UTC:

    15 0 * * *  docker exec hermes-trader python /app/scripts/reconcile_fills.py \\
                    --window-hours 26 --alert-on-orphan >> /data/reconcile.log 2>&1

Exit codes:
  0  clean (no orphans)
  2  one or more orphans/phantoms detected
  3  API / configuration error (could not reconcile)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

HL_API = os.environ.get("HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("reconcile")


# ── Exchange access ─────────────────────────────────────────────────────

def resolve_user() -> str:
    return (os.environ.get("HYPERLIQUID_MASTER_ADDRESS")
            or os.environ.get("HYPERLIQUID_WALLET_ADDRESS", ""))


def fetch_user_fills(user: str, limit: int = 200) -> List[Dict[str, Any]]:
    payload = json.dumps(
        {"type": "userFills", "user": user, "limit": limit}
    ).encode()
    req = urllib.request.Request(
        f"{HL_API}/info", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected userFills response: {data!r}")
    return data


# ── Fill classification ─────────────────────────────────────────────────

def _fill_side_dir(f: Dict[str, Any]) -> str:
    """'open' if this fill increased/increases a position, 'close' if it
    carries realized PnL. A fill with closedPnl != 0 is a reducing/close
    fill by Hyperliquid's definition; the rest are opens (including partial
    adds)."""
    try:
        if float(f.get("closedPnl", 0) or 0) != 0.0:
            return "close"
    except (TypeError, ValueError):
        pass
    return "open"


def _side_label(raw: str) -> str:
    return "long" if raw == "B" else "short"


# ── Reconciliation ──────────────────────────────────────────────────────

def reconcile(window_hours: float) -> Dict[str, Any]:
    user = resolve_user()
    if not user:
        raise RuntimeError(
            "HYPERLIQUID_WALLET_ADDRESS / MASTER_ADDRESS not configured")

    # Import lazily so the module can be unit-tested without the full
    # hermes package path on disk.
    from hermes_trader.agents.memory import memory
    memory.load()

    fills = fetch_user_fills(user, limit=200)
    cutoff_ms = int((time.time() - window_hours * 3600) * 1000)
    recent = [f for f in fills if int(f.get("time", 0) or 0) >= cutoff_ms]

    opens = [f for f in recent if _fill_side_dir(f) == "open"]
    closes = [f for f in recent if _fill_side_dir(f) == "close"]

    local_trades = memory.get_all_trades()
    local_closes = memory.get_closes(limit=500)

    # Index local records.
    trade_oids = {str(t.get("order_id")) for t in local_trades
                  if t.get("order_id") is not None}
    # Local closes carry close_oid (exchange_trigger backfill) — match on it.
    close_oids = {str(c.get("close_oid")) for c in local_closes
                   if c.get("close_oid") is not None}
    # Secondary key: coin + close-time bucket (±2 min) for closes recorded
    # via paths that don't yet store close_oid.
    close_buckets = {
        (c.get("coin"), int((c.get("closed_at") or 0) // 120000))
        for c in local_closes if c.get("coin") and c.get("closed_at")
    }

    orphan_opens: List[Dict[str, Any]] = []
    for f in opens:
        oid = str(f.get("oid", ""))
        if oid and oid not in trade_oids:
            orphan_opens.append(f)

    orphan_closes: List[Dict[str, Any]] = []
    for f in closes:
        oid = str(f.get("oid", ""))
        coin = f.get("coin")
        try:
            bucket = (coin, int(int(f.get("time", 0)) // 120000))
        except (TypeError, ValueError):
            bucket = None
        if oid and oid in close_oids:
            continue
        if bucket and bucket in close_buckets:
            continue
        orphan_closes.append(f)

    # Phantom locals: a local close whose time falls in the window but no
    # exchange close fill exists for the same coin. (Conservative: only
    # flags the most recent 24h, where userFills coverage is complete.)
    phantom_closes: List[Dict[str, Any]] = []
    day_cutoff_ms = int((time.time() - 24 * 3600) * 1000)
    ex_close_coins = {f.get("coin") for f in closes}
    for c in local_closes:
        if int(c.get("closed_at") or 0) < day_cutoff_ms:
            continue
        if c.get("coin") not in ex_close_coins:
            phantom_closes.append(c)

    return {
        "user": user,
        "window_hours": window_hours,
        "exchange_fills_total": len(fills),
        "exchange_fills_in_window": len(recent),
        "exchange_opens": len(opens),
        "exchange_closes": len(closes),
        "local_trades": len(local_trades),
        "local_closes": len(local_closes),
        "orphan_opens": orphan_opens,
        "orphan_closes": orphan_closes,
        "phantom_closes": phantom_closes,
    }


# ── Auto-backfill ───────────────────────────────────────────────────────

def backfill_orphan_close(f: Dict[str, Any]) -> None:
    """Record an exchange-side close fill that memory never captured, via
    the same record_close chokepoint the live loop uses (so it also reaches
    events.jsonl). Mirrors trading_loop.resolve_close_fill backfill logic."""
    from hermes_trader.agents.memory import memory
    from hermes_trader.session_log import append as _log_event
    coin = f.get("coin", "")
    side = _side_label(f.get("side", ""))
    exit_px = float(f.get("px") or 0.0)
    sz = abs(float(f.get("sz") or 0.0))
    closed_pnl = float(f.get("closedPnl") or 0.0)
    fee = float(f.get("fee") or 0.0)
    closed_at = int(f.get("time") or int(time.time() * 1000))
    # Entry px is unknown from a standalone fill; record 0 and flag source
    # so dashboards/backfills can treat it as a reconciliation-sourced row.
    notional = sz * exit_px
    _net_usd = round(closed_pnl - notional * 0.00025, 4)
    memory.record_close({
        "coin": coin, "side": side,
        "entry_px": 0.0, "exit_px": exit_px,
        "size_coin": sz, "notional_usd": round(notional, 4),
        "spot_pct": 0.0,
        "realized_pnl_pct": 0.0,
        "realized_pnl_usd": _net_usd,
        "gross_pnl_usd": round(closed_pnl + fee, 4),
        "fee_usd": round(fee, 4),
        "leverage": 1,
        "closed_at": closed_at,
        "entry_time": None,
        "hold_minutes": None,
        "close_source": "reconcile_backfill",
        "close_oid": f.get("oid"),
    })
    # Also emit a session-log dsl_exit event so the web dashboard's
    # closed-trades panel (which reads session-log only) can see this row.
    try:
        _log_event({
            "event": "dsl_exit",
            "coin": coin,
            "side": side,
            "leverage": 1,
            "reason": "reconcile_backfill",
            "exit_reason": "reconcile_backfill",
            "hold_min": None,
            "unrealized_pct": 0.0,
            "leveraged_pct": 0.0,
            "executed": True,
            "detail": f"reconcile oid={f.get('oid')}",
            "fill_px": exit_px,
            "entry_px": 0.0,
            "realized_spot_pct": 0.0,
            "realized_pnl_pct": 0.0,
            "fees_pct": 0.0005,
            "close_source": "reconcile_backfill",
            "ts": closed_at,
        })
    except Exception as _le:
        logger.warning(f"[reconcile] session-log write failed: {_le}")
    logger.warning(
        f"[reconcile] backfilled orphan close {coin} {side} @ {exit_px} "
        f"oid={f.get('oid')} pnl=${closed_pnl}")


# ── Reporting ───────────────────────────────────────────────────────────

def print_report(r: Dict[str, Any]) -> None:
    print("=" * 64)
    print(f"Reconciliation report  —  {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    print(f"wallet: {r['user']}")
    print(f"window: last {r['window_hours']}h")
    print("-" * 64)
    print(f"exchange fills (total / in-window): "
          f"{r['exchange_fills_total']} / {r['exchange_fills_in_window']}")
    print(f"  open fills:  {r['exchange_opens']}")
    print(f"  close fills: {r['exchange_closes']}")
    print(f"local trades: {r['local_trades']}")
    print(f"local closes: {r['local_closes']}")
    print("-" * 64)
    print(f"ORPHAN OPENS  (exchange fill, no local trade): {len(r['orphan_opens'])}")
    for f in r["orphan_opens"]:
        print(f"  - {f.get('coin'):>10} {f.get('side')} "
              f"px={f.get('px')} sz={f.get('sz')} oid={f.get('oid')} "
              f"time={f.get('time')}")
    print(f"ORPHAN CLOSES (exchange close, no local record): "
          f"{len(r['orphan_closes'])}")
    for f in r["orphan_closes"]:
        print(f"  - {f.get('coin'):>10} {f.get('side')} "
              f"px={f.get('px')} pnl={f.get('closedPnl')} "
              f"oid={f.get('oid')} time={f.get('time')}")
    print(f"PHANTOM CLOSES (local record, no exchange fill): "
          f"{len(r['phantom_closes'])}")
    for c in r["phantom_closes"]:
        print(f"  - {c.get('coin'):>10} {c.get('side')} "
              f"exit={c.get('exit_px')} src={c.get('close_source')}")
    print("=" * 64)


def build_alert_text(r: Dict[str, Any]) -> str:
    """Compact Feishu text payload for orphan/phantom discrepancies."""
    lines: List[str] = [
        "⚠️ Hermes 对账告警 — 孤儿交易",
        f"钱包: {r['user']}",
        f"窗口: 最近 {r['window_hours']}h  "
        f"(交易所成交 {r['exchange_fills_in_window']} / "
        f"本地 trades {r['local_trades']} / closes {r['local_closes']})",
    ]
    if r["orphan_opens"]:
        lines.append(f"\n孤儿开仓 {len(r['orphan_opens'])} 笔 "
                     f"(交易所有成交、本地无记录):")
        for f in r["orphan_opens"]:
            lines.append(
                f"  • {f.get('coin')} {f.get('side')} "
                f"px={f.get('px')} sz={f.get('sz')} oid={f.get('oid')}")
    if r["orphan_closes"]:
        lines.append(f"\n孤儿平仓 {len(r['orphan_closes'])} 笔 "
                     f"(交易所已平仓、本地无 close 记录 — PURR 类故障):")
        for f in r["orphan_closes"]:
            lines.append(
                f"  • {f.get('coin')} {f.get('side')} "
                f"px={f.get('px')} pnl={f.get('closedPnl')} oid={f.get('oid')}")
    if r["phantom_closes"]:
        lines.append(f"\n幻影平仓 {len(r['phantom_closes'])} 笔 "
                     f"(本地有记录、交易所无对应成交):")
        for c in r["phantom_closes"]:
            lines.append(
                f"  • {c.get('coin')} {c.get('side')} "
                f"src={c.get('close_source')}")
    lines.append("\n请核查: 可能为手工/外部下单或事件记录丢失。"
                 "确认后可用 --auto-backfill 补录孤儿平仓。")
    return "\n".join(lines)


def send_alert(r: Dict[str, Any]) -> bool:
    """Push the discrepancy alert through Feishu if configured."""
    text = build_alert_text(r)
    try:
        from hermes_trader.notify import send_text
        return bool(send_text(text, category="risk"))
    except Exception as e:
        logger.warning(f"[reconcile] Feishu alert unavailable: {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-hours", type=float, default=26.0,
                        help="Look-back window for exchange fills (default 26h).")
    parser.add_argument("--auto-backfill", action="store_true",
                        help="Append orphan closes to memory via record_close.")
    parser.add_argument("--alert-on-orphan", action="store_true",
                        help="Exit non-zero when any orphan/phantom is found.")
    args = parser.parse_args()

    try:
        r = reconcile(args.window_hours)
    except Exception as e:
        logger.error(f"reconciliation failed: {e}")
        return 3

    print_report(r)

    if args.auto_backfill and r["orphan_closes"]:
        for f in r["orphan_closes"]:
            try:
                backfill_orphan_close(f)
            except Exception as e:
                logger.error(f"backfill failed for oid={f.get('oid')}: {e}")

    n_issues = (len(r["orphan_opens"]) + len(r["orphan_closes"])
                + len(r["phantom_closes"]))
    if n_issues and args.alert_on_orphan:
        logger.warning(
            f"[reconcile] {n_issues} discrepancy(ies) detected — alerting")
        if send_alert(r):
            logger.info("[reconcile] Feishu alert sent")
        return 2
    if n_issues:
        logger.warning(f"[reconcile] {n_issues} discrepancy(ies) (no --alert)")
    else:
        logger.info("[reconcile] clean: exchange fills match local records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
