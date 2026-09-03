"""Format session-log events into Feishu cards and push them.

Registered as a best-effort subscriber of ``session_log.append`` so every
subsystem — the trading loop, the FastAPI server, DSL exits, risk gates —
gets a single, consistent notification path without each call site having to
know about Feishu.

Only high-signal event types are pushed. High-frequency/noisy events
(scan / ta_skip / near_miss / loop_heartbeat) are intentionally dropped here;
the surge detector notifies on those through its own richer card.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from hermes_trader import notify

logger = logging.getLogger("hermes_trader.notify_dispatch")

# Verdicts worth a ping. PASS / HOLD / no-action are too noisy.
_ACTIONABLE_VERDICTS = {"LONG", "SHORT", "CLOSE"}

_VERDICT_CN = {"LONG": "做多", "SHORT": "做空", "CLOSE": "平仓"}
_NEWS_RISK_CN = {"none": "无", "positive": "正面", "negative": "负面"}


def _v(v: Any) -> str:
    """空值占位。"""
    if v is None or v == "":
        return "—"
    return str(v)


def dispatch(record: dict[str, Any]) -> None:
    """Translate one session-log record into a Feishu card (best-effort)."""
    try:
        event = record.get("event")
        if event == "execute":
            _on_execute(record)
        elif event == "ai_close":
            _on_ai_close(record)
        elif event == "dsl_exit":
            _on_dsl_exit(record)
        elif event == "hard_killswitch":
            _on_killswitch(record)
        elif event == "place_order":
            _on_manual_order(record)
        elif event == "close_position":
            _on_manual_close(record)
        elif event == "research":
            _on_research(record)
        elif event == "error":
            _on_error(record)
        elif event == "loop_start":
            _on_loop_start(record)
        elif event == "loop_stop":
            notify.send_card("交易循环已停止（用户手动停止）",
                             category="system", level="warn")
        elif event == "ws_status":
            _on_ws_status(record)
        # scan / ta_skip / near_miss / loop_heartbeat: deliberately ignored.
    except Exception:  # never let notification break the caller
        # P1-17: this was logged at debug, so a malformed record (None where a
        # float f-string expected) silently swallowed high-severity alerts
        # (e.g. hard killswitch). Surface it and count it for /metrics.
        logger.exception("notify_dispatch failed for event=%r record=%r",
                         record.get("event"), record)
        try:
            from hermes_trader import metrics

            metrics.NOTIFY_DISPATCH_ERRORS.inc()
        except Exception:
            pass


def _on_execute(r: dict[str, Any]) -> None:
    coin = r.get("coin", "?")
    if r.get("executed"):
        fields = {
            "币种": coin,
            "方向": r.get("side"),
            "金额": f"${r.get('size_usd'):.0f}" if r.get("size_usd") is not None else "—",
            "入场价": r.get("entry_px"),
            "止损": r.get("stop_px"),
            "止盈": r.get("tp_px"),
            "市场状态": r.get("regime"),
        }
        notify.send_card(f"开仓成交 — {coin}", fields=fields,
                         category="trade", level="success")
    else:
        blocked = r.get("blocked_by")
        reason = r.get("detail")
        if isinstance(blocked, list):
            blocked = ", ".join(blocked)
        fields = {
            "币种": coin,
            "方向": r.get("side"),
            "拦截原因": blocked or reason or "未知",
        }
        notify.send_card(f"开仓被风控拦截 — {coin}", fields=fields,
                         category="signal", level="warn",
                         dedup_key=f"blocked:{coin}")


def _on_ai_close(r: dict[str, Any]) -> None:
    coin = r.get("coin", "?")
    fields = {
        "币种": coin,
        "结果": "已平仓" if r.get("executed") else "未成交/无需平仓",
        "详情": r.get("detail"),
    }
    reasoning = (r.get("reasoning") or "").strip()
    notify.send_card(f"AI 决策平仓 — {coin}", fields=fields,
                     markdown=(f"**理由**\n{reasoning[:500]}" if reasoning else ""),
                     category="trade", level="info")


def _on_dsl_exit(r: dict[str, Any]) -> None:
    coin = r.get("coin", "?")
    realized = r.get("realized_pnl_pct")
    fields = {
        "币种": coin,
        "方向": r.get("side"),
        "杠杆": f"{r.get('leverage')}x" if r.get("leverage") else "—",
        "触发原因": r.get("reason"),
        "仓位盈亏": f"{r.get('leveraged_pct'):+.2f}%" if r.get("leveraged_pct") is not None else "—",
        "已实现盈亏": f"{realized:+.2f}%" if realized is not None else "—",
    }
    level = "danger" if (realized is not None and realized < 0) else "warn"
    notify.send_card(f"止损/止盈平仓 — {coin}", fields=fields,
                     category="trade", level=level)


def _on_killswitch(r: dict[str, Any]) -> None:
    # None-safe: a malformed killswitch record (daily_pnl/limit missing) must
    # not raise and get swallowed — that would drop the single most important
    # alert. Missing numerics render as $0.
    pnl = r.get("daily_pnl")
    limit = r.get("limit")
    fields = {
        "当日盈亏": f"${(pnl or 0):.2f}",
        "亏损上限": f"${(limit or 0):.0f}",
        "强平仓位": r.get("flattened"),
    }
    notify.send_card("硬日亏熔断触发 — 已全仓平仓",
                     fields=fields, category="risk", level="danger")


def _on_manual_order(r: dict[str, Any]) -> None:
    notify.send_card(f"手动下单 — {r.get('coin', '?')}",
                     fields={"币种": r.get("coin"),
                             "方向": r.get("side"),
                             "结果": "成功" if r.get("ok") else "失败"},
                     category="trade", level="info")


def _on_manual_close(r: dict[str, Any]) -> None:
    notify.send_card(f"手动平仓 — {r.get('coin', '?')}",
                     fields={"币种": r.get("coin"),
                             "结果": "成功" if r.get("ok") else "失败"},
                     category="trade", level="info")


def _on_research(r: dict[str, Any]) -> None:
    verdict = str(r.get("verdict", "")).upper()
    if verdict not in _ACTIONABLE_VERDICTS:
        return
    coin = r.get("coin", "?")
    verdict_cn = _VERDICT_CN.get(verdict, verdict)
    news_risk_raw = str(r.get("news_risk") or "none").lower()
    fields = {
        "币种": coin,
        "结论": verdict_cn,
        "置信度": f"{r.get('confidence')}%" if r.get("confidence") is not None else "—",
        "新闻风险": _NEWS_RISK_CN.get(news_risk_raw, _v(r.get("news_risk"))),
        "建议入场": _v(r.get("entry_px")),
        "止损": _v(r.get("stop_px")),
        "止盈": _v(r.get("tp_px")),
    }
    reasoning = (r.get("reasoning") or "").strip()
    notify.send_card(f"AI 决策 — {verdict_cn} {coin}", fields=fields,
                     markdown=(f"**推理**\n{reasoning[:600]}" if reasoning else ""),
                     category="ai", level="info",
                     dedup_key=f"research:{coin}:{verdict}")


def _on_error(r: dict[str, Any]) -> None:
    scope = r.get("scope") or r.get("coin") or "loop"
    fields = {"来源": scope, "错误": r.get("error")}
    # Watchdog hangs and DSL monitor failures are the most dangerous runtime
    # faults (no stop protection / stuck process) — mark them danger.
    danger_scopes = {"watchdog", "dsl_monitor"}
    level = "danger" if scope in danger_scopes else "warn"
    notify.send_card(f"系统错误 — {scope}", fields=fields,
                     category="system", level=level,
                     dedup_key=f"error:{scope}")


def _on_loop_start(r: dict[str, Any]) -> None:
    cfg = r.get("config") or {}
    scan = cfg.get("scan") or {}
    mode_raw = str(cfg.get("mode") or "").upper()
    mode_cn = {"LIVE": "实盘", "OFF": "暂停", "SHADOW": "影子"}.get(mode_raw, _v(cfg.get("mode")))
    fields = {
        "模式": mode_cn,
        "扫描间隔": f"{r.get('scan_interval')}s" if r.get("scan_interval") else "—",
        "交易门槛分": _v(scan.get("minCompositeScore")),
        "暴涨通知分": os.environ.get("HERMES_SURGE_MIN_SCORE", "40"),
    }
    notify.send_card("Hermes 交易系统已启动", fields=fields,
                     category="system", level="info")


# Phase 4 P0-3: feed-status transitions. The trading loop only emits this on
# an EDGE after a 30s hysteresis window (see realtime_feed.FeedStatusTracker),
# so a card here means the WS mid feed genuinely changed state — not a frame
# blip. ``down`` is the fail-closed posture: the af14 feed gate independently
# blocks new entries on stale mids; this card is the human-facing signal.
_FEED_STATUS_CN = {"ok": "正常（WS 实时）", "degraded": "降级（WS 断连，已切 REST）",
                   "down": "中断（WS+REST 双失败）"}


def _on_ws_status(r: dict[str, Any]) -> None:
    status = str(r.get("status", "")).lower()
    if status not in _FEED_STATUS_CN:
        return
    previous = str(r.get("previous", "")).lower()
    fields = {
        "当前状态": _FEED_STATUS_CN[status],
        "上一状态": _FEED_STATUS_CN.get(previous, _v(previous or "—")),
        "WS 数据龄期": f"{r.get('ws_age_s')}s" if r.get("ws_age_s") is not None else "无数据",
        "REST 数据龄期": f"{r.get('rest_age_s')}s" if r.get("rest_age_s") is not None else "无数据",
    }
    if status == "down":
        title = "行情馈送中断 — 已停止开新仓"
        level = "danger"
        detail = ("WS 与 REST 均无法获取有效行情，系统已 fail-closed："
                  "停止开新仓；持仓止损仍由本地监控独立执行。请立即检查网络。")
    elif status == "degraded":
        title = "行情馈送降级 — 已切换 REST 轮询"
        level = "warn"
        detail = "WS allMids 中断，扫描已自动降速并回退 REST 快照；平仓感知延迟增大。"
    else:  # ok — recovery
        title = "行情馈送恢复 — WS 实时通道已恢复"
        level = "success"
        detail = "WS allMids 恢复实时推送，扫描周期与平仓感知恢复低延迟。"
    # Distinct dedup key per transition direction so a recovery card is never
    # throttled by the preceding degradation card; 10-min dedup still absorbs
    # any unexpected same-direction repeat.
    notify.send_card(title, fields=fields, markdown=f"**说明**\n{detail}",
                     category="system", level=level,
                     dedup_key=f"ws_status:{previous}->{status}")
