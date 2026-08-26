#!/usr/bin/env python3
"""Hermes 每日交易报告 (P3-16).

Reads the authoritative append-only event log
(``~/.hermes-trading/events.jsonl``), aggregates the day's signals / orders /
closes / risk decisions into a compact markdown report, and delivers it to one
or more channels:

  * stdout (always)
  * Feishu/Lark custom bot webhook        (FEISHU_WEBHOOK / --feishu)
  * Telegram bot                          (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID / --telegram)
  * DingTalk custom robot webhook         (DINGTALK_WEBHOOK / --dingtalk)
  * WeCom (企业微信) group bot webhook    (WECOM_WEBHOOK / --wecom)
  * Generic JSON webhook                  (REPORT_WEBHOOK_URL / --webhook)

Designed to be invoked by the Hermes Agent cron scheduler (no_agent, zero LLM
cost), e.g. once per day shortly after UTC midnight. It is read-only and never
places orders.

Usage:
    python3 scripts/daily_report.py                      # today UTC, print only
    python3 scripts/daily_report.py --date 2026-08-13    # specific day
    python3 scripts/daily_report.py --push               # deliver to configured channels
    python3 scripts/daily_report.py --feishu https://open.feishu.cn/open-apis/bot/v2/hook/xxx
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

EVENTS_FILE = Path(os.environ.get(
    "DAILY_REPORT_EVENTS_FILE",
    os.environ.get(
        "SESSION_LOG_PATH",
        # Current trading loop writes the append-only session log here (mounted,
        # persistent volume). HERMES_EVENTS_FILE may point at the separate HTA
        # signal feed, so it must NOT be the default for the trading daily
        # report — it has a different schema (no execute/dsl_exit events).
        "/data/session-log.jsonl",
    ),
))


# ── Event aggregation ────────────────────────────────────────────────────

def _parse_ts(rec: Dict[str, Any]) -> Optional[datetime]:
    """Parse a record's timestamp.

    Supports the current session-log format (millisecond ``ts`` integer) and
    the legacy ISO-8601 ``timestamp`` string with a trailing Z.
    """
    # Current format: millisecond epoch under "ts".
    ts = rec.get("ts")
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        except Exception:
            return None
    # Legacy format: ISO-8601 string under "timestamp".
    raw = rec.get("timestamp", "")
    if not raw:
        return None
    try:
        if isinstance(raw, str) and raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def load_day_events(day: str) -> List[Dict[str, Any]]:
    """Return all event records whose UTC timestamp falls on ``day`` (YYYY-MM-DD).

    Normalizes the current flat session-log schema (events like ``scan``,
    ``research``, ``execute``, ``ta_skip``, ``near_miss`` with millisecond
    ``ts``) onto the legacy (signal/order/close/risk) buckets the renderer
    understands, so one report covers both formats.
    """
    if not EVENTS_FILE.exists():
        return []
    out: List[Dict[str, Any]] = []
    with EVENTS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(rec)
            if ts is None:
                continue
            if ts.astimezone(timezone.utc).strftime("%Y-%m-%d") != day:
                continue
            out.append(_normalize(rec))
    return out


# Legacy event names kept as aliases so historical logs still aggregate. The
# current trading loop emits the names on the right-hand side.
_EVENT_ALIAS = {
    "ai_close": "dsl_exit",
    "position_close": "dsl_exit",
    "risk_gate": "risk",
}


def _normalize(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a record onto ``{event, payload}`` shape (copy, never in place)."""
    rec = dict(rec)
    ev = rec.get("event")
    if ev in _EVENT_ALIAS:
        rec["event"] = _EVENT_ALIAS[ev]
    # Legacy payload was nested under "payload"; current schema is flat. Surface
    # the fields the aggregator/renderer read as a payload dict when absent.
    if not isinstance(rec.get("payload"), dict):
        payload = {k: v for k, v in rec.items()
                   if k not in ("ts", "timestamp", "event", "trace_id")}
        rec["payload"] = payload
    # Ticker fallback (legacy used "ticker"; current uses "coin"). Scan events
    # carry no single coin — they have "coins"/"coin_scores" — so they must not
    # get a ticker here or they all collapse into a bogus "?" bucket.
    if not rec["payload"].get("ticker") and rec["payload"].get("coin"):
        rec["payload"]["ticker"] = rec["payload"]["coin"]
    rec["_ts"] = _parse_ts(rec)
    return rec


def _f(val: Any) -> float:
    """Coerce to float, mapping None/garbage to 0.0."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _block_reason(payload: Dict[str, Any]) -> str:
    """Human-readable reason an execute attempt did not fill."""
    blocked = payload.get("blocked_by")
    if isinstance(blocked, list) and blocked:
        return "; ".join(str(b) for b in blocked)
    if isinstance(blocked, str) and blocked.strip():
        return blocked.strip()
    detail = payload.get("detail")
    return str(detail).strip() if detail else "unspecified"


def aggregate(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate a day's events into reportable counters and totals.

    Strict semantics (agreed report definitions):
      * 研判 (research) — only ``research`` events, i.e. real AI verdicts.
      * 成交 (filled)   — only ``execute`` events with ``executed is True``.
      * 被拦 (blocked)  — ``execute`` events with ``executed`` falsy.
      * 平仓 (closes)   — ``dsl_exit`` events, the only close event emitted.
      * USD 盈亏        — last ``loop_heartbeat.daily_pnl`` of the day, the
        account-level figure; ``dsl_exit`` carries percentages only.
    """
    research: List[Dict[str, Any]] = []
    filled: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    closes: List[Dict[str, Any]] = []
    risks: List[Dict[str, Any]] = []
    near_misses: List[Dict[str, Any]] = []
    ta_skips: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    killswitches: List[Dict[str, Any]] = []

    n_scans = 0
    n_scans_with_trigger = 0
    scan_coin_hits: Dict[str, int] = {}

    last_heartbeat: Optional[Dict[str, Any]] = None
    daily_pnl_min: Optional[float] = None
    daily_pnl_max: Optional[float] = None

    for rec in events:
        ev = rec.get("event")
        payload = rec.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        payload = dict(payload)
        payload["_trace_id"] = rec.get("trace_id", "")
        payload["_ts"] = rec.get("_ts")

        if ev == "scan":
            n_scans += 1
            triggers = int(_f(payload.get("triggers")))
            if triggers > 0:
                n_scans_with_trigger += 1
            # Ticker attribution belongs to scan hits, not to a single "coin".
            for cs in payload.get("coin_scores") or []:
                if isinstance(cs, dict) and cs.get("coin"):
                    c = str(cs["coin"])
                    scan_coin_hits[c] = scan_coin_hits.get(c, 0) + 1
        elif ev == "research":
            research.append(payload)
        elif ev == "execute":
            (filled if payload.get("executed") else blocked).append(payload)
        elif ev == "dsl_exit":
            closes.append(payload)
        elif ev == "near_miss":
            near_misses.append(payload)
        elif ev == "ta_skip":
            ta_skips.append(payload)
        elif ev == "error":
            errors.append(payload)
        elif ev == "hard_killswitch":
            killswitches.append(payload)
        elif ev == "risk":
            risks.append(payload)
        elif ev == "loop_heartbeat":
            last_heartbeat = payload
            pnl = _f(payload.get("daily_pnl"))
            daily_pnl_min = pnl if daily_pnl_min is None else min(daily_pnl_min, pnl)
            daily_pnl_max = pnl if daily_pnl_max is None else max(daily_pnl_max, pnl)

    # Realized PnL in USD is an account-level figure; dsl_exit has no USD field.
    realized_usd = _f((last_heartbeat or {}).get("daily_pnl"))

    pct_wins = [_f(c.get("realized_pnl_pct")) for c in closes
                if _f(c.get("realized_pnl_pct")) > 0]
    pct_losses = [abs(_f(c.get("realized_pnl_pct"))) for c in closes
                  if _f(c.get("realized_pnl_pct")) <= 0]
    win_rate = (len(pct_wins) / len(closes)) if closes else 0.0

    risk_blocked = sum(1 for r in risks if str(r.get("verdict", "")).lower() == "block")
    risk_fail_open = sum(1 for r in risks if str(r.get("verdict", "")).lower() == "fail_open")
    risk_approved = sum(1 for r in risks if str(r.get("verdict", "")).lower() in ("approve", "approved"))

    research_by_ticker: Dict[str, int] = {}
    for s in research:
        t = s.get("ticker") or s.get("coin") or "?"
        research_by_ticker[t] = research_by_ticker.get(t, 0) + 1

    near_miss_by_ticker: Dict[str, int] = {}
    for nm in near_misses:
        t = nm.get("ticker") or nm.get("coin") or "?"
        near_miss_by_ticker[t] = near_miss_by_ticker.get(t, 0) + 1

    # Some errors carry a "scope" (e.g. watchdog); coin-level failures carry
    # only "coin", so fall back to it before giving up.
    error_by_scope: Dict[str, int] = {}
    for e in errors:
        scope = str(e.get("scope") or e.get("coin") or "?")
        error_by_scope[scope] = error_by_scope.get(scope, 0) + 1

    verdicts: Dict[str, int] = {}
    for r in research:
        v = str(r.get("verdict") or "?").upper()
        verdicts[v] = verdicts.get(v, 0) + 1

    return {
        "total_events": len(events),
        "research": research,
        "filled": filled,
        "blocked": blocked,
        "closes": closes,
        "risks": risks,
        "near_misses": near_misses,
        "ta_skips": ta_skips,
        "errors": errors,
        "killswitches": killswitches,
        "n_scans": n_scans,
        "n_scans_with_trigger": n_scans_with_trigger,
        "n_research": len(research),
        "n_filled": len(filled),
        "n_blocked": len(blocked),
        "n_closes": len(closes),
        "n_near_miss": len(near_misses),
        "n_ta_skip": len(ta_skips),
        "n_errors": len(errors),
        "realized_usd": realized_usd,
        "daily_pnl_min": daily_pnl_min,
        "daily_pnl_max": daily_pnl_max,
        "equity": _f((last_heartbeat or {}).get("equity")) if last_heartbeat else None,
        "open_positions": (last_heartbeat or {}).get("open_positions"),
        "config": (last_heartbeat or {}).get("config"),
        "win_rate": win_rate,
        "avg_win_pct": (sum(pct_wins) / len(pct_wins)) if pct_wins else 0.0,
        "avg_loss_pct": (sum(pct_losses) / len(pct_losses)) if pct_losses else 0.0,
        "risk_blocked": risk_blocked,
        "risk_fail_open": risk_fail_open,
        "risk_approved": risk_approved,
        "scan_coin_hits": scan_coin_hits,
        "research_by_ticker": research_by_ticker,
        "near_miss_by_ticker": near_miss_by_ticker,
        "error_by_scope": error_by_scope,
        "verdicts": verdicts,
    }


# ── Rendering ────────────────────────────────────────────────────────────

def _top(counter: Dict[str, int], n: int = 8) -> str:
    if not counter:
        return "—"
    top = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return ", ".join(f"{k}×{v}" for k, v in top)


def _hhmm(ts: Optional[datetime]) -> str:
    return ts.astimezone(timezone.utc).strftime("%H:%M") if ts else "--:--"


def _close_lines(agg: Dict[str, Any], limit: int = 20) -> List[str]:
    """One line per close, percentages only (dsl_exit has no USD field)."""
    out: List[str] = []
    for c in agg["closes"][-limit:]:
        ticker = c.get("ticker") or c.get("coin") or "?"
        side = str(c.get("side") or "").upper()
        pnl_pct = _f(c.get("realized_pnl_pct"))
        spot_pct = _f(c.get("realized_spot_pct"))
        reason = str(c.get("reason") or "").split("(")[0].strip() or "—"
        mark = "🟢" if pnl_pct > 0 else "🔴"
        out.append(
            f"{mark} {_hhmm(c.get('_ts'))} {ticker} {side} "
            f"{pnl_pct:+.2f}% (spot {spot_pct:+.2f}%) · {reason}"
        )
    return out


def _blocked_lines(agg: Dict[str, Any], limit: int = 10) -> List[str]:
    out: List[str] = []
    for b in agg["blocked"][-limit:]:
        ticker = b.get("ticker") or b.get("coin") or "?"
        out.append(f"{_hhmm(b.get('_ts'))} {ticker} — {_block_reason(b)[:160]}")
    return out


def _filled_lines(agg: Dict[str, Any], limit: int = 10) -> List[str]:
    out: List[str] = []
    for o in agg["filled"][-limit:]:
        ticker = o.get("ticker") or o.get("coin") or "?"
        side = str(o.get("side") or "").upper() or "—"
        out.append(
            f"{_hhmm(o.get('_ts'))} {ticker} {side} "
            f"size {_f(o.get('size_usd')):.2f} USD @ {_f(o.get('entry_px')):g}"
        )
    return out


def _research_lines(agg: Dict[str, Any], limit: int = 10) -> List[str]:
    out: List[str] = []
    for r in agg["research"][-limit:]:
        ticker = r.get("ticker") or r.get("coin") or "?"
        verdict = str(r.get("verdict") or "?").upper()
        out.append(
            f"{_hhmm(r.get('_ts'))} {ticker} → {verdict} "
            f"(conf {_f(r.get('confidence')):.2f})"
        )
    return out


def _level(agg: Dict[str, Any]) -> str:
    """Card colour: danger on killswitch, warn on net loss, else info/success."""
    if agg["killswitches"]:
        return "danger"
    pnl = _f(agg["realized_usd"])
    if pnl < 0:
        return "warn"
    if pnl > 0:
        return "success"
    return "info"


def render_markdown(day: str, agg: Dict[str, Any]) -> str:
    """Plain-text/markdown report for stdout and non-Feishu channels."""
    lines: List[str] = []
    lines.append(f"# Hermes 每日交易报告 — {day} (UTC)")
    lines.append("")

    if not agg["total_events"]:
        lines.append("_当日无事件记录（可能未运行或无交易）。_")
        return "\n".join(lines)

    equity = agg["equity"]
    lines.append(f"- 事件总数: **{agg['total_events']}**")
    lines.append(
        f"- 扫描 {agg['n_scans']} 轮（命中 {agg['n_scans_with_trigger']}）"
        f"  |  研判 {agg['n_research']}"
        f"  |  成交 {agg['n_filled']}  |  被拦 {agg['n_blocked']}"
        f"  |  平仓 {agg['n_closes']}"
    )
    lines.append(
        f"- 当日盈亏: **{agg['realized_usd']:+.4f} USD**"
        + (f"  |  账户权益 {equity:.4f} USD" if equity is not None else "")
    )
    if agg["daily_pnl_min"] is not None:
        lines.append(
            f"- 盈亏区间: 低 {agg['daily_pnl_min']:+.4f} / 高 {agg['daily_pnl_max']:+.4f} USD"
        )
    if agg["n_closes"]:
        lines.append(
            f"- 平仓表现: 胜率 {agg['win_rate'] * 100:.0f}%"
            f", 均赢 {agg['avg_win_pct']:+.2f}%, 均亏 -{agg['avg_loss_pct']:.2f}%"
        )
    lines.append(
        f"- 过滤: near_miss {agg['n_near_miss']}  |  ta_skip {agg['n_ta_skip']}"
        f"  |  错误 {agg['n_errors']}"
    )
    if agg["open_positions"] is not None:
        lines.append(f"- 收盘持仓: {agg['open_positions']}")

    if agg["verdicts"]:
        lines.append(f"- 研判结论: {_top(agg['verdicts'])}")
    if agg["scan_coin_hits"]:
        lines.append(f"- 扫描命中 Top: {_top(agg['scan_coin_hits'])}")
    if agg["near_miss_by_ticker"]:
        lines.append(f"- near_miss Top: {_top(agg['near_miss_by_ticker'])}")
    if agg["error_by_scope"]:
        lines.append(f"- 错误来源: {_top(agg['error_by_scope'])}")

    for title, body in (
        ("平仓明细", _close_lines(agg)),
        ("成交明细", _filled_lines(agg)),
        ("被拦下单", _blocked_lines(agg)),
        ("AI 研判", _research_lines(agg)),
    ):
        if body:
            lines.append("")
            lines.append(f"## {title}")
            lines.extend(f"- {b}" for b in body)

    if agg["killswitches"]:
        lines.append("")
        lines.append("## ⚠️ 熔断触发")
        for k in agg["killswitches"]:
            lines.append(
                f"- {_hhmm(k.get('_ts'))} daily_pnl {_f(k.get('daily_pnl')):+.2f} "
                f"超出限额 {_f(k.get('limit')):+.2f}"
                f"{'，已强平' if k.get('flattened') else ''}"
            )

    if agg["config"]:
        lines.append("")
        lines.append(f"_配置: {json.dumps(agg['config'], ensure_ascii=False)}_")

    return "\n".join(lines)


def render_card(day: str, agg: Dict[str, Any]) -> Dict[str, Any]:
    """Build the Feishu interactive-card arguments for ``notify.send_card``."""
    if not agg["total_events"]:
        return {
            "title": f"Hermes 每日交易报告 — {day} (UTC)",
            "level": "info",
            "fields": {"事件总数": 0},
            "markdown": "**当日无事件记录**（可能未运行或无交易）。",
        }

    equity = agg["equity"]
    fields: Dict[str, Any] = {
        "当日盈亏": f"{agg['realized_usd']:+.4f} USD",
        "账户权益": f"{equity:.4f} USD" if equity is not None else "—",
        "扫描 / 命中": f"{agg['n_scans']} / {agg['n_scans_with_trigger']}",
        "AI 研判": agg["n_research"],
        "成交 / 被拦": f"{agg['n_filled']} / {agg['n_blocked']}",
        "平仓": agg["n_closes"],
        "near_miss / ta_skip": f"{agg['n_near_miss']} / {agg['n_ta_skip']}",
        "错误数": agg["n_errors"],
    }
    if agg["n_closes"]:
        fields["胜率"] = f"{agg['win_rate'] * 100:.0f}% ({agg['n_closes']} 笔)"
        fields["均赢 / 均亏"] = f"{agg['avg_win_pct']:+.2f}% / -{agg['avg_loss_pct']:.2f}%"
    if agg["open_positions"] is not None:
        fields["收盘持仓"] = agg["open_positions"]
    fields["事件总数"] = agg["total_events"]

    blocks: List[str] = []

    if agg["killswitches"]:
        ks = "\n".join(
            f"- {_hhmm(k.get('_ts'))} daily_pnl {_f(k.get('daily_pnl')):+.2f} "
            f"超限 {_f(k.get('limit')):+.2f}"
            f"{'，已强平' if k.get('flattened') else ''}"
            for k in agg["killswitches"]
        )
        blocks.append(f"**🚨 熔断触发**\n{ks}")

    for title, body in (
        ("📉 平仓明细", _close_lines(agg, 12)),
        ("✅ 成交明细", _filled_lines(agg, 8)),
        ("🚫 被拦下单", _blocked_lines(agg, 8)),
        ("🧠 AI 研判", _research_lines(agg, 8)),
    ):
        if body:
            blocks.append(f"**{title}**\n" + "\n".join(f"- {b}" for b in body))

    dist: List[str] = []
    if agg["verdicts"]:
        dist.append(f"研判结论 {_top(agg['verdicts'])}")
    if agg["scan_coin_hits"]:
        dist.append(f"扫描命中 {_top(agg['scan_coin_hits'], 6)}")
    if agg["near_miss_by_ticker"]:
        dist.append(f"near_miss {_top(agg['near_miss_by_ticker'], 6)}")
    if agg["error_by_scope"]:
        dist.append(f"错误来源 {_top(agg['error_by_scope'], 6)}")
    if dist:
        blocks.append("**📊 分布**\n" + "\n".join(f"- {d}" for d in dist))

    if agg["daily_pnl_min"] is not None:
        blocks.append(
            f"盈亏区间 低 {agg['daily_pnl_min']:+.4f} / 高 {agg['daily_pnl_max']:+.4f} USD"
        )
    if agg["config"]:
        blocks.append(f"配置 `{json.dumps(agg['config'], ensure_ascii=False)}`")

    return {
        "title": f"Hermes 每日交易报告 — {day} (UTC)",
        "level": _level(agg),
        "fields": fields,
        "markdown": "\n\n".join(blocks),
    }


# ── Delivery channels ────────────────────────────────────────────────────

def _http_post_json(url: str, payload: Dict[str, Any], timeout: float = 10.0) -> Tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return 200 <= resp.status < 300, f"HTTP {resp.status}: {body[:200]}"
    except Exception as e:
        return False, str(e)


def push_feishu(webhook: str, card: Dict[str, Any]) -> Tuple[bool, str]:
    # Use the shared notify module so HMAC signing (FEISHU_WEBHOOK_SECRET),
    # category gating, and the interactive-card layout are applied consistently.
    # No dedup_key is passed: the daily report must never hit the 10-min throttle.
    try:
        from hermes_trader import notify
        if not notify.is_enabled("report"):
            return False, "feishu webhook not configured or 'report' category disabled"
        ok = notify.send_card(
            card["title"],
            card.get("fields"),
            category="report",
            level=card.get("level", "info"),
            markdown=card.get("markdown", ""),
        )
        return ok, "OK" if ok else "send failed (see logs)"
    except Exception as e:
        return False, f"notify error: {e}"


def push_dingtalk(webhook: str, text: str) -> Tuple[bool, str]:
    return _http_post_json(webhook, {"msgtype": "text", "text": {"content": text}})


def push_wecom(webhook: str, text: str) -> Tuple[bool, str]:
    return _http_post_json(webhook, {"msgtype": "text", "text": {"content": text}})


def push_generic(webhook: str, text: str, day: str) -> Tuple[bool, str]:
    return _http_post_json(webhook, {"date": day, "report": text, "source": "hermes-daily-report"})


def push_telegram(token: str, chat_id: str, text: str) -> Tuple[bool, str]:
    # Telegram messages capped at 4096 chars; truncate if necessary.
    body = text[:4000]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    return _http_post_json(url, {"chat_id": chat_id, "text": body, "parse_mode": "Markdown"})


def deliver(
    text: str,
    day: str,
    *,
    push: bool,
    card: Optional[Dict[str, Any]] = None,
    feishu: str = "",
    telegram_token: str = "",
    telegram_chat: str = "",
    dingtalk: str = "",
    wecom: str = "",
    webhook: str = "",
) -> List[str]:
    """Deliver the report to configured channels. Returns status lines.

    Feishu gets the interactive card; every other channel keeps plain text.
    """
    statuses: List[str] = []
    if not push:
        return statuses

    if feishu and card:
        ok, msg = push_feishu(feishu, card)
        statuses.append(f"feishu: {'OK' if ok else 'FAIL — ' + msg}")
    if dingtalk:
        ok, msg = push_dingtalk(dingtalk, text)
        statuses.append(f"dingtalk: {'OK' if ok else 'FAIL — ' + msg}")
    if wecom:
        ok, msg = push_wecom(wecom, text)
        statuses.append(f"wecom: {'OK' if ok else 'FAIL — ' + msg}")
    if webhook:
        ok, msg = push_generic(webhook, text, day)
        statuses.append(f"webhook: {'OK' if ok else 'FAIL — ' + msg}")
    if telegram_token and telegram_chat:
        ok, msg = push_telegram(telegram_token, telegram_chat, text)
        statuses.append(f"telegram: {'OK' if ok else 'FAIL — ' + msg}")

    if not statuses:
        statuses.append("push requested but no channels configured (set FEISHU_WEBHOOK / TELEGRAM_BOT_TOKEN / DINGTALK_WEBHOOK / WECOM_WEBHOOK / REPORT_WEBHOOK_URL)")
    return statuses


# ── CLI ──────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes daily trading report (P3-16)")
    parser.add_argument("--date", help="Report day YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--push", action="store_true", help="Deliver to configured channels")
    parser.add_argument("--feishu", default=os.environ.get(
        "FEISHU_WEBHOOK", os.environ.get("FEISHU_WEBHOOK_URL", "")))
    parser.add_argument("--dingtalk", default=os.environ.get("DINGTALK_WEBHOOK", ""))
    parser.add_argument("--wecom", default=os.environ.get("WECOM_WEBHOOK", ""))
    parser.add_argument("--webhook", default=os.environ.get("REPORT_WEBHOOK_URL", ""))
    parser.add_argument("--telegram-token", default=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    parser.add_argument("--telegram-chat", default=os.environ.get("TELEGRAM_CHAT_ID", ""))
    args = parser.parse_args()

    day = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not EVENTS_FILE.exists():
        print(f"[daily_report] events file not found: {EVENTS_FILE}", file=sys.stderr)
        return 1

    events = load_day_events(day)
    agg = aggregate(events)
    report = render_markdown(day, agg)
    print(report)

    statuses = deliver(
        report, day, push=args.push,
        card=render_card(day, agg),
        feishu=args.feishu,
        telegram_token=args.telegram_token,
        telegram_chat=args.telegram_chat,
        dingtalk=args.dingtalk,
        wecom=args.wecom,
        webhook=args.webhook,
    )
    for s in statuses:
        print(f"[push] {s}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
