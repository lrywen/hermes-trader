#!/usr/bin/env python3
"""Surge postmortem generator.

Watches the live scan stream for a *surge* — a coin whose composite score
explodes across cycles — and, the moment one is detected, writes a full
postmortem to BOTH:

  * the container log (prominent ``[SURGE_POSTMORTEM]`` banner), so anyone
    tailing ``docker logs -f`` sees it in real time, and
  * a markdown file under ``/data/postmortems/`` (the mounted, persistent
    volume) so the report survives restarts and can be diffed/archived.

The report pulls the recent candle window, the full trigger breakdown, and
the coin's session-log decision trail (scan / research / execute / near_miss)
so "why did we miss / catch this move?" is answerable immediately.

False-positive guard
--------------------
A naive "score jumped >= N points" rule fires on ordinary bar-to-bar noise
(a coin oscillating 30->46 every cycle as one trigger flips). Detection here
requires ALL of:

  1. absolute strength  — current composite >= ``min_score`` (gate, default 54),
                          i.e. it is a real actionable signal, not just a bump;
  2. novelty            — the coin was NOT above ``min_score`` in the previous
                          cycle (first cross into actionable territory);
  3. acceleration       — delta(score) >= ``min_jump`` (default 15) vs the
                          previous cycle, OR there was no prior observation
                          (first time we see it already surging);
  4. momentum trigger   - one of the fast-move triggers fired
                          (momentumBurst / breakout / pctMoveSpike), confirming
                          the score jump is driven by price action, not a
                          slow trend-strength drift;
  5. cooldown           - the same coin won't re-report for ``cooldown_s``
                          (default 1800s) so a persistent signal doesn't spam.

Library + CLI: call ``SurgeDetector.observe(coin, score, triggers)`` from the
trading loop after each scan, or run ``python surge_postmortem.py --simulate``
to generate a sample report for verification.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hermes_trader import notify

logger = logging.getLogger("hermes_trader.surge_postmortem")

# Persistent, mounted volume. Overridable via env for tests.
POSTMORTEM_DIR = Path(os.environ.get("HERMES_POSTMORTEM_DIR", "/data/postmortems"))
SESSION_LOG = Path(os.environ.get("HERMES_SESSION_LOG", "/data/session-log.jsonl"))
# Per-coin previous-composite snapshot. Without it every container restart
# reports "composite none -> X (jump n/a)": the whole novelty/acceleration test
# is skipped and the report loses the one number that explains the surge.
STATE_FILE = Path(os.environ.get(
    "HERMES_SURGE_STATE_FILE", "/data/.surge-detector-state.json"))
# Discard a snapshot older than this: a score from hours ago is not a
# meaningful "previous cycle" to diff against, and treating it as one would
# fabricate a jump that never happened.
STATE_MAX_AGE_S = float(os.environ.get("HERMES_SURGE_STATE_MAX_AGE_S", "3600"))

# Fast-move triggers that confirm the score jump is driven by actual price
# action rather than a slow drift in trend-strength indicators.
_MOMENTUM_TRIGGERS = {
    "momentumBurst", "breakout", "pctMoveSpike",
    "trendFlip1h", "volumeSpike",
}

# Trigger name → 中文标签
TRIGGER_ZH = {
    "pctMoveSpike": "涨幅飙升",
    "volumeSpike": "成交量激增",
    "breakout": "突破",
    "rangeCompression": "区间压缩",
    "trendStrength": "趋势强度",
    "momentumBurst": "动量爆发",
    "volumeBuildup1h": "1h 量能积累",
    "trendFlip1h": "1h 趋势翻转",
    "higherLows1h": "1h 高点抬升",
    "uptrendMomentum": "上升趋势动量",
    "downtrendMomentum": "下降趋势动量",
    "momentumContinuation1h": "1h 动量延续",
    "dailyMover": "日内异动",
}

# Feishu custom bot webhook. If FEISHU_WEBHOOK_URL is unset, push is silently
# disabled (postmortem still goes to container log + markdown).
FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
FEISHU_WEBHOOK_SECRET = os.environ.get("FEISHU_WEBHOOK_SECRET", "").strip()
# Base URL for the "view full report" button in Feishu cards. Should point at
# the Hermes web server reachable from the operator's phone/browser, e.g.
# http://192.168.124.65:8000 or a public reverse-proxy URL.
HERMES_BASE_URL = os.environ.get("HERMES_BASE_URL", "").strip().rstrip("/")


def send_feishu_card(
    coin: str,
    score: float,
    prev_score: Optional[float],
    fired_names: set,
    report_path: Optional[Path],
    mid: Optional[float] = None,
) -> bool:
    """Send an interactive card to the configured Feishu group webhook.

    Returns True on success. Never raises — failures are logged and swallowed
    so notification delivery can never block trading.
    """
    if not FEISHU_WEBHOOK_URL:
        return False

    jump = f"{score - prev_score:+.1f}" if prev_score is not None else "首次观测"
    prev = f"{prev_score:.1f}" if prev_score is not None else "无"
    triggers_str = "、".join(
        TRIGGER_ZH.get(n, n) for n in sorted(fired_names)
    ) or "无"

    fields = {
        "币种": coin,
        "中间价": mid if mid is not None else "—",
        "分数跳变": f"{prev} → {score:.1f} ({jump})",
        "动量触发器": triggers_str,
    }
    markdown = ""
    if report_path is not None:
        markdown = f"**复盘报告**\n`{report_path}`"
    return notify.send_card(
        title=f"暴涨复盘 — {coin}",
        fields=fields,
        category="surge",
        level="danger",
        markdown=markdown,
        button_text="📄 一键查看完整报告" if report_path is not None else "",
        button_url=notify.postmortem_url(report_path.name) if report_path else "",
    )


@dataclass
class SurgeConfig:
    min_score: float = 40.0       # notify threshold (independent of trade gate 54)
    min_jump: float = 15.0        # and have jumped at least this many points
    cooldown_s: float = 1800.0    # no re-report per coin for 30 min
    history_window: int = 12      # session-log events to trail per coin
    candle_count: int = 24        # candles to embed in the report
    candle_interval: str = "5m"
    # Render + notify off the caller's thread. The live loop calls observe()
    # once per surfaced coin inside its tick, and a report does a candle fetch,
    # a full session-log scan and a Feishu POST — seconds of latency the trading
    # tick must not pay. Tests/CLI set this False (or call drain()) when they
    # need the report file to exist the moment observe() returns.
    async_report: bool = True
    # Persist the per-coin previous-composite map so a restart still knows what
    # last cycle looked like. Tests/CLI disable it to stay hermetic.
    persist_state: bool = True
    state_flush_s: float = 30.0   # coalesce writes; observe() runs per coin per tick


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_ts_ms(ms: Optional[float]) -> str:
    if not ms:
        return "?"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class SurgeDetector:
    """Stateful per-coin surge watcher. One instance lives for the loop run."""

    def __init__(self, config: Optional[SurgeConfig] = None) -> None:
        self.cfg = config or SurgeConfig()
        # coin -> {"score": float, "ts": float}
        self._last: dict[str, dict[str, float]] = {}
        # coin -> last report epoch seconds
        self._last_report: dict[str, float] = {}
        # Live async report workers, so drain() can wait for them.
        self._workers: list[threading.Thread] = []
        self._workers_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state_dirty = False
        self._state_flushed_at = 0.0
        if self.cfg.persist_state:
            self._load_state()

    # ---- previous-composite persistence (P0-3) ----

    def _load_state(self) -> None:
        """Restore the per-coin snapshot written by a previous process."""
        try:
            if not STATE_FILE.exists():
                return
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            now = time.time()
            last = data.get("last") or {}
            reports = data.get("last_report") or {}
            kept = 0
            for coin, rec in last.items():
                if not isinstance(rec, dict):
                    continue
                try:
                    ts = float(rec.get("ts", 0.0))
                    score = float(rec.get("score"))
                except (TypeError, ValueError):
                    continue
                if now - ts > STATE_MAX_AGE_S:
                    continue
                self._last[str(coin)] = {"score": score, "ts": ts}
                kept += 1
            for coin, ts in reports.items():
                try:
                    ts_f = float(ts)
                except (TypeError, ValueError):
                    continue
                # Keep cooldowns only while they can still be active.
                if now - ts_f < self.cfg.cooldown_s:
                    self._last_report[str(coin)] = ts_f
            logger.info(
                "[SURGE_POSTMORTEM] restored %d/%d coin snapshots from %s "
                "(%d active cooldowns)",
                kept, len(last), STATE_FILE, len(self._last_report))
        except Exception as e:
            # A corrupt snapshot must not stop the loop from starting; we just
            # fall back to the old cold-start behaviour.
            logger.warning(f"[SURGE_POSTMORTEM] state load failed: {e!r}")

    def _flush_state(self, force: bool = False) -> None:
        """Atomically write the snapshot, coalesced to state_flush_s."""
        if not self.cfg.persist_state:
            return
        with self._state_lock:
            if not self._state_dirty:
                return
            now = time.time()
            if not force and (now - self._state_flushed_at) < self.cfg.state_flush_s:
                return
            payload = {
                "version": 1,
                "saved_at": now,
                "last": dict(self._last),
                "last_report": dict(self._last_report),
            }
            self._state_dirty = False
            self._state_flushed_at = now
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(STATE_FILE)   # atomic: never leave a half-written file
        except Exception as e:
            logger.warning(f"[SURGE_POSTMORTEM] state flush failed: {e!r}")

    def observe(
        self,
        coin: str,
        score: float,
        triggers: Optional[list[dict[str, Any]]] = None,
        perception: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Feed one cycle's reading for a coin. Returns True if a surge fired.

        Safe to call from the loop; never raises (failures are logged and
        swallowed so postmortem logic can never block trading).
        """
        try:
            now = time.time()
            triggers = triggers or []
            fired_names = {
                str(t.get("name")) for t in triggers if isinstance(t, dict) and t.get("fired")
            }

            prev = self._last.get(coin)
            prev_score = float(prev["score"]) if prev else None
            self._last[coin] = {"score": float(score), "ts": now}
            self._state_dirty = True
            self._flush_state()

            if not self._is_surge(score, prev_score, fired_names):
                return False

            # Cooldown: don't re-spam a persistent signal.
            last_rep = self._last_report.get(coin, 0.0)
            if (now - last_rep) < self.cfg.cooldown_s:
                logger.debug(
                    "[SURGE_POSTMORTEM] %s surge detected but in cooldown "
                    "(%.0fs since last report)", coin, now - last_rep)
                return False
            self._last_report[coin] = now
            # Force: a lost cooldown means a duplicate alert after restart.
            self._state_dirty = True
            self._flush_state(force=True)

            # Announce on the caller's thread: the banner IS the alert, and it
            # must not wait behind candle fetch / log scan / webhook.
            self._emit_banner(coin, score, prev_score, fired_names, None)

            if self.cfg.async_report:
                self._spawn_report(coin, score, prev_score, triggers,
                                   perception, fired_names)
            else:
                self._build_and_notify(coin, score, prev_score, triggers,
                                       perception, fired_names)
            return True
        except Exception as e:  # never let observability break the loop
            logger.warning(f"[SURGE_POSTMORTEM] observe failed for {coin}: {e!r}")
            return False

    def _spawn_report(
        self,
        coin: str,
        score: float,
        prev_score: Optional[float],
        triggers: list[dict[str, Any]],
        perception: Optional[dict[str, Any]],
        fired_names: set,
    ) -> None:
        t = threading.Thread(
            target=self._build_and_notify,
            args=(coin, score, prev_score, triggers, perception, fired_names),
            name=f"surge-postmortem-{coin}",
            daemon=True,
        )
        with self._workers_lock:
            self._workers = [w for w in self._workers if w.is_alive()]
            self._workers.append(t)
        t.start()

    def _build_and_notify(
        self,
        coin: str,
        score: float,
        prev_score: Optional[float],
        triggers: list[dict[str, Any]],
        perception: Optional[dict[str, Any]],
        fired_names: set,
    ) -> None:
        """Render the report and push the card. Never raises."""
        try:
            report_path = generate_postmortem(
                coin=coin,
                score=score,
                prev_score=prev_score,
                triggers=triggers,
                perception=perception,
                config=self.cfg,
            )
            if report_path is not None:
                logger.warning("[SURGE_POSTMORTEM] %s full report: %s", coin, report_path)
            mid = (perception or {}).get("mid")
            send_feishu_card(coin, score, prev_score, fired_names,
                             report_path, mid=mid)
        except Exception as e:
            logger.warning(f"[SURGE_POSTMORTEM] report build failed for {coin}: {e!r}")

    def drain(self, timeout: float = 30.0) -> bool:
        """Wait for in-flight async reports. Returns True if all finished.

        For tests/CLI that assert on the written markdown right after observe().
        """
        with self._workers_lock:
            workers = list(self._workers)
        deadline = time.monotonic() + timeout
        for w in workers:
            w.join(max(0.0, deadline - time.monotonic()))
        return not any(w.is_alive() for w in workers)

    def _is_surge(
        self,
        score: float,
        prev_score: Optional[float],
        fired_names: set,
    ) -> bool:
        # 1) absolute strength — must be a real actionable signal
        if score < self.cfg.min_score:
            return False
        # 2) novelty — wasn't already above gate last cycle (persistent signal
        #    isn't a surge; a FIRST cross is). If no history, treat as novel.
        if prev_score is not None and prev_score >= self.cfg.min_score:
            return False
        # 3) acceleration — big jump vs previous cycle, or first observation
        if prev_score is not None and (score - prev_score) < self.cfg.min_jump:
            return False
        # 4) momentum confirmation — a fast-move trigger must be responsible
        if not (fired_names & _MOMENTUM_TRIGGERS):
            return False
        return True

    @staticmethod
    def _emit_banner(
        coin: str,
        score: float,
        prev_score: Optional[float],
        fired_names: set,
        report_path: Optional[Path],
    ) -> None:
        jump = f"{score - prev_score:+.1f}" if prev_score is not None else "n/a"
        prev = f"{prev_score:.1f}" if prev_score is not None else "none"
        banner = "=" * 64
        logger.warning(banner)
        logger.warning("[SURGE_POSTMORTEM] %s  composite %s -> %.1f (jump %s)",
                       coin, prev, score, jump)
        logger.warning("[SURGE_POSTMORTEM] momentum triggers fired: %s",
                       ", ".join(sorted(fired_names)) or "none")
        if report_path is not None:
            logger.warning("[SURGE_POSTMORTEM] full report: %s", report_path)
        logger.warning(banner)


# ── Report generation ─────────────────────────────────────────────────────

def _recent_candles(coin: str, cfg: SurgeConfig) -> list[dict[str, Any]]:
    """Best-effort fetch of recent closed candles for context. Empty on error."""
    try:
        from hermes_trader.client.hl_client import fetch_hl_candles
        # opportunistic: this is a report illustration, not a trading input. It
        # must never park on the shared per-IP rate budget that the live loop's
        # own candleSnapshot calls (weight 20 each) need.
        raw = fetch_hl_candles(
            coin,
            interval=cfg.candle_interval,
            count=cfg.candle_count,
            opportunistic=True,
        )
        out = []
        for c in raw or []:
            out.append({
                "t": int(getattr(c, "t")),
                "o": float(getattr(c, "o")),
                "h": float(getattr(c, "h")),
                "l": float(getattr(c, "l")),
                "c": float(getattr(c, "c")),
                "v": float(getattr(c, "v")),
            })
        return out
    except Exception as e:
        logger.warning(f"[SURGE_POSTMORTEM] candle fetch failed for {coin}: {e!r}")
        return []


def _coin_trail(coin: str, limit: int) -> list[dict[str, Any]]:
    """Recent session-log events for this coin (most recent first-ish)."""
    if not SESSION_LOG.exists():
        return []
    trail: list[dict[str, Any]] = []
    try:
        # Stream the whole append-only log; it's small (~18k lines / 5MB).
        with SESSION_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("coin") == coin or coin in (rec.get("coins") or []):
                    trail.append(rec)
    except Exception as e:
        logger.warning(f"[SURGE_POSTMORTEM] session-log read failed: {e!r}")
        return []
    return trail[-limit:]


def _render_markdown(
    coin: str,
    score: float,
    prev_score: Optional[float],
    triggers: list[dict[str, Any]],
    perception: Optional[dict[str, Any]],
    candles: list[dict[str, Any]],
    trail: list[dict[str, Any]],
    cfg: SurgeConfig,
) -> str:
    now = _utc_now()
    jump = f"{score - prev_score:+.1f}" if prev_score is not None else "首次观测"
    prev = f"{prev_score:.1f}" if prev_score is not None else "无"

    # Trigger name → Chinese label (unknown triggers keep their raw name).
    trigger_zh = TRIGGER_ZH
    # Event name → Chinese label.
    event_zh = {
        "scan": "扫描",
        "research": "研究",
        "execute": "执行",
        "ta_skip": "技术分析跳过",
        "near_miss": "未达标",
        "error": "错误",
        "dsl_exit": "DSL 退出",
        "hard_killswitch": "紧急熔断",
        "ai_close": "AI 平仓",
        "loop_start": "循环启动",
        "loop_heartbeat": "心跳",
    }

    def _zh_trigger(name: Any) -> str:
        n = str(name)
        return trigger_zh.get(n, n)

    def _zh_event(name: Any) -> str:
        n = str(name)
        return event_zh.get(n, n)

    _VERDICT_CN = {"LONG": "做多", "SHORT": "做空", "CLOSE": "平仓", "PASS": "观望"}
    _SIDE_CN = {"long": "做多", "short": "做空", "buy": "买入", "sell": "卖出"}
    _BLOCKED_CN = {
        "cooldown": "冷却期",
        "score_below_gate": "评分未达门槛",
        "composite_too_low": "综合评分过低",
        "max_positions": "持仓数已达上限",
        "daily_loss_limit": "日亏损限额",
        "killswitch": "紧急熔断",
        "news_risk_high": "新闻风险过高",
        "no_atr_no_stop": "无 ATR 无法设止损",
        "insufficient_margin": "保证金不足",
        "already_in_position": "已持仓",
        "opposite_position": "反向持仓",
        "min_confidence": "置信度不足",
        "entry_too_close": "入场点过近",
        "spread_too_wide": "点差过大",
        "risk_reward_too_low": "盈亏比不足",
        "leverage_cap": "杠杆上限",
        "notional_cap": "名义金额上限",
        "fail_closed_global": "熔断全局关闭",
        "fail_closed_shorts": "熔断禁止做空",
        "http_error": "风控服务 HTTP 错误",
        "blocked": "被风控拦截",
        "blocked_bypass": "被风控拦截（绕过）",
        "shadow_mode_would_execute": "影子模式（未真实执行）",
        "no_atr_no_stop": "无 ATR 无法设止损",
    }

    def _zh_verdict(v: Any) -> str:
        if v is None or v == "":
            return "—"
        return _VERDICT_CN.get(str(v).upper(), str(v))

    def _zh_side(v: Any) -> str:
        if v is None or v == "":
            return "—"
        return _SIDE_CN.get(str(v).lower(), str(v))

    def _zh_blocked(v: Any) -> str:
        if v is None or v == "":
            return "—"
        if isinstance(v, list):
            return "、".join(_BLOCKED_CN.get(str(x), str(x)) for x in v)
        return _BLOCKED_CN.get(str(v), str(v))

    # TA 跳过信号（ta_skip.signal）中文映射
    _SIGNAL_CN = {
        "LONG": "做多", "SHORT": "做空", "PASS": "观望", "CLOSE": "平仓",
        "long": "做多", "short": "做空", "pass": "观望", "close": "平仓",
        "REJECTED": "拒绝（技术面硬否决）",
        "NEUTRAL": "中性", "WAIT": "等待", "HOLD": "持有",
        "NO_TRADE": "不交易", "SKIP": "跳过",
    }
    # 未达标阈值（near_miss.gate）中文映射
    _GATE_CN = {
        "min_score": "最低综合分",
        "min_confidence": "最低置信度",
        "min_jump": "最小跳升",
        "cooldown": "冷却期",
        "score_below_gate": "评分未达门槛",
        "composite_too_low": "综合评分过低",
        "momentum": "动量确认",
        "trend": "趋势确认",
        "atr": "ATR 止损",
        "risk_reward": "盈亏比",
        "spread": "点差",
    }

    def _zh_signal(v: Any) -> str:
        if v is None or v == "":
            return "—"
        s = str(v)
        # 形如 "REJECTED (TA hard veto)" 拆括号翻译
        return _SIGNAL_CN.get(s, s)

    def _zh_gate(v: Any) -> str:
        if v is None or v == "":
            return "—"
        s = str(v)
        return _GATE_CN.get(s, s)

    # 未知事件原始 JSON 的字段名 → 中文标签
    _FIELD_CN = {
        "signal": "信号", "score": "评分", "gate": "阈值",
        "verdict": "结论", "confidence": "置信度",
        "side": "方向", "executed": "已执行", "blocked_by": "拦截原因",
        "detail": "详情", "reason": "原因", "triggers": "触发数",
        "coins": "币种", "ticker": "币种", "symbol": "标的",
        "entry_px": "入场价", "stop_px": "止损价", "tp_px": "止盈价",
        "px": "价格", "price": "价格", "mid": "中间价",
        "size": "数量", "qty": "数量", "notional": "名义金额",
        "error": "错误", "msg": "消息", "message": "消息",
        "level": "级别", "source": "来源", "scope": "范围",
        "position": "持仓", "pnl": "盈亏", "equity": "净值",
    }

    def _zh_flatten(obj: Any) -> str:
        """将未知事件的扁平 dict 渲染成中文化的 key=value 文本。"""
        if not isinstance(obj, dict):
            return "" if obj is None else str(obj)
        parts: list[str] = []
        for k, v in obj.items():
            if k in ("ts", "event", "coin", "coins"):
                continue
            label = _FIELD_CN.get(k, k)
            if isinstance(v, bool):
                val = "是" if v else "否"
            elif isinstance(v, (list, tuple)):
                val = "、".join(str(x) for x in v) if v else "—"
            elif v is None or v == "":
                val = "—"
            elif k in ("signal", "verdict"):
                val = _zh_signal(v)
            elif k in ("side",):
                val = _zh_side(v)
            elif k == "blocked_by":
                val = _zh_blocked(v)
            elif k == "gate":
                val = _zh_gate(v)
            elif k == "reason" or k == "detail":
                val = _zh_reason(v) or "—"
            else:
                val = str(v)
            parts.append(f"{label}={val}")
        return " ".join(parts)[:200]

    def _zh_reason(reason: Any) -> str:
        """将 trigger reason 英文描述翻译为中文（保留数值占位）。"""
        if not reason:
            return ""
        s = str(reason)
        # 先做精确子串替换（长串优先），避免误伤
        replacements = [
            ("return spike up", "涨幅飙升（向上）"),
            ("return spike down", "跌幅飙升（向下）"),
            ("volume spike", "成交量激增"),
            ("breakout above", "向上突破"),
            ("breakout below", "向下跌破"),
            ("on weak volume", "（量能不足）"),
            ("unconfirmed", "未确认"),
            ("fakeout risk", "假突破风险"),
            ("but not held", "但未站稳"),
            ("bars", "根K线"),
            ("bar high", "根K线高点"),
            ("bar low", "根K线低点"),
            ("inside range", "区间内运行"),
            ("BB squeeze", "布林带收窄"),
            ("BB normal", "布林带正常"),
            ("trending", "趋势中"),
            ("(late/extended)", "（趋势末期/过度延伸）"),
            ("over", "于"),
            ("uptrend", "上升趋势"),
            ("downtrend", "下降趋势"),
            ("flat", "平稳"),
            ("sparse", "数据稀疏"),
            ("no_baseline", "无基准"),
            ("insufficient_history", "历史数据不足"),
            ("no recent cross", "近期无交叉"),
            ("cross up", "金叉向上"),
            ("higher lows", "低点抬升"),
            ("(need", "（需要"),
            ("prior 20h baseline", "前 20 小时基准"),
            ("4h vol", "4小时量能"),
            ("12h uptrend", "12 小时上升趋势"),
            ("pullback", "回踩"),
            ("EMA-stacked", "EMA 多头排列"),
            ("stacked=", "多头排列="),
            ("shooting_star", "射击之星"),
            ("bearish_engulfing", "看跌吞没"),
            ("hammer", "锤子线"),
            ("bullish_engulfing", "看涨吞没"),
            ("after", "出现于"),
            ("strong trend", "强趋势"),
            ("RVOL", "相对成交量"),
            ("[squeeze-resolved]", "（收窄已突破）"),
            ("not a configured 24h mover", "非配置的 24h 异动币种"),
            ("24h / vol", "24h / 成交量"),
            ("no_atr_no_stop", "无 ATR 无法设止损"),
            ("insufficient candle history to size a stop", "K线历史不足，无法计算止损"),
            ("notional_below_min", "名义金额低于最小值"),
            ("min_size_ge_position", "最小下单量≥持仓量"),
            ("min_size_ge_90pct", "最小下单量≥持仓量 90%"),
        ]
        for en, zh in replacements:
            s = s.replace(en, zh)
        return s

    lines: list[str] = []
    lines.append(f"# 🚨 暴涨复盘 — {coin}")
    lines.append("")
    lines.append(f"- **生成时间**：{now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"- **综合评分**：**{score:.1f}**（上周期 {prev}，跳升 {jump}；阈值 {cfg.min_score:.0f}）")
    mid = (perception or {}).get("mid")
    if mid is not None:
        lines.append(f"- **当前价格**：{mid}")
    fired = [t for t in triggers if isinstance(t, dict) and t.get("fired")]
    fired_str = "、".join(f"{_zh_trigger(t.get('name'))}({t.get('score')})" for t in fired)
    lines.append(f"- **触发指标（{len(fired)} 个）**：{fired_str}")
    lines.append("")

    # Trigger breakdown
    lines.append("## 指标明细")
    lines.append("")
    lines.append("| 指标 | 分数 | 是否触发 | 原因 |")
    lines.append("|---|---|---|---|")
    for t in triggers:
        if not isinstance(t, dict):
            continue
        reason = _zh_reason(t.get("reason")).replace("|", "/")[:80]
        lines.append(f"| {_zh_trigger(t.get('name'))} | {t.get('score')} | "
                     f"{'✅ 是' if t.get('fired') else '否'} | {reason} |")
    lines.append("")

    # Candle price action
    if candles:
        closes = [c["c"] for c in candles]
        first_c, last_c = closes[0], closes[-1]
        move_pct = ((last_c - first_c) / first_c * 100) if first_c else 0.0
        hi = max(c["h"] for c in candles)
        lo = min(c["l"] for c in candles)
        lines.append(f"## 价格走势（最近 {len(candles)} 根 {cfg.candle_interval} K线）")
        lines.append("")
        lines.append(f"- **区间涨跌**：**{move_pct:+.2f}%** "
                     f"（{first_c} → {last_c}）")
        lines.append(f"- **区间最高/最低**：{hi} / {lo}")
        lines.append("")
        lines.append("| 时间 (UTC) | 开盘 | 最高 | 最低 | 收盘 | 成交量 |")
        lines.append("|---|---|---|---|---|---|")
        for c in candles[-10:]:
            lines.append(
                f"| {_fmt_ts_ms(c['t'])} | {c['o']} | {c['h']} | {c['l']} | {c['c']} | {c['v']:.2f} |"
            )
        lines.append("")
    else:
        lines.append("## 价格走势")
        lines.append("")
        lines.append("_K线数据获取失败，暂无价格走势。_")
        lines.append("")

    # Decision trail
    lines.append(f"## 决策轨迹（{coin} 最近 {len(trail)} 条事件）")
    lines.append("")
    if not trail:
        lines.append("_该币种暂无历史决策事件。_")
    else:
        for rec in trail:
            ts = _fmt_ts_ms(rec.get("ts"))
            ev = rec.get("event")
            detail = ""
            if ev == "scan":
                detail = f"触发数={rec.get('triggers')} 币种={rec.get('coins')}"
            elif ev == "research":
                detail = (f"结论={_zh_verdict(rec.get('verdict'))} 置信度={rec.get('confidence')} "
                          f"入场={rec.get('entry_px') or '—'} 止损={rec.get('stop_px') or '—'} "
                          f"止盈={rec.get('tp_px') or '—'}")
            elif ev == "execute":
                executed = "是" if rec.get("executed") else "否"
                detail = (f"已执行={executed} 方向={_zh_side(rec.get('side'))} "
                          f"拦截原因={_zh_blocked(rec.get('blocked_by'))} "
                          f"详情={_zh_reason(rec.get('detail')) or '—'}")
            elif ev == "ta_skip":
                detail = f"信号={_zh_signal(rec.get('signal'))} 评分={rec.get('score') or '—'}"
            elif ev == "near_miss":
                detail = f"评分={rec.get('score') or '—'} 阈值={_zh_gate(rec.get('gate'))}"
            else:
                detail = _zh_flatten(rec) or "—"
            lines.append(f"- `{ts}` **{_zh_event(ev)}** — {detail}")
    lines.append("")
    lines.append("---")
    lines.append("_由 Hermes Trader 暴涨复盘模块自动生成。_")
    return "\n".join(lines)


def generate_postmortem(
    coin: str,
    score: float,
    prev_score: Optional[float],
    triggers: list[dict[str, Any]],
    perception: Optional[dict[str, Any]] = None,
    config: Optional[SurgeConfig] = None,
) -> Optional[Path]:
    """Build and persist a markdown postmortem. Returns the file path or None."""
    cfg = config or SurgeConfig()
    try:
        POSTMORTEM_DIR.mkdir(parents=True, exist_ok=True)
        candles = _recent_candles(coin, cfg)
        trail = _coin_trail(coin, cfg.history_window)
        md = _render_markdown(coin, score, prev_score, triggers, perception,
                              candles, trail, cfg)

        stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        path = POSTMORTEM_DIR / f"surge-{coin}-{stamp}.md"
        path.write_text(md, encoding="utf-8")
        logger.info("[SURGE_POSTMORTEM] wrote %s (%d bytes)", path, len(md))
        return path
    except Exception as e:
        logger.warning(f"[SURGE_POSTMORTEM] generate failed for {coin}: {e!r}")
        return None


# ── CLI / simulation ──────────────────────────────────────────────────────

def _simulate() -> int:
    """Inject a synthetic surge and confirm the report is generated.

    Does NOT touch real coin data; builds a fake perception + triggers so the
    rendering/emit path is verified end-to-end without waiting for a real move.
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    # Synchronous + stateless: the assertions below read the report file right
    # after observe() returns, and a simulation must not pollute the live
    # per-coin snapshot with SIMBTC.
    cfg = SurgeConfig(async_report=False, persist_state=False)
    det = SurgeDetector(cfg)

    fake_triggers = [
        {"name": "trendStrength", "score": 20.0, "fired": True, "reason": "strong trend"},
        {"name": "momentumBurst", "score": 18.0, "fired": True,
         "reason": "5m return > 2.0ATR in 3 bars"},
        {"name": "breakout", "score": 16.5, "fired": True,
         "reason": "20-bar high + RVOL 3.1x"},
        {"name": "volumeSpike", "score": 10.0, "fired": True, "reason": "RVOL 3.1x"},
        {"name": "pctMoveSpike", "score": 0.0, "fired": False, "reason": ""},
    ]

    # Cycle 1: low score (no surge) — seeds prev_score.
    det.observe("SIMBTC", 33.0, [t for t in fake_triggers if t["name"] != "momentumBurst"])
    # Cycle 2: cross gate with big jump + momentum trigger -> SURGE.
    fired = det.observe(
        "SIMBTC", 58.5, fake_triggers,
        perception={"coin": "SIMBTC", "mid": 65000.0, "composite_score": 58.5},
    )
    print(f"\nSimulation surge fired: {fired}")
    if fired:
        reports = sorted(POSTMORTEM_DIR.glob("surge-SIMBTC-*.md"))
        if reports:
            print(f"Report file: {reports[-1]}")
    return 0 if fired else 1


def _test_feishu() -> int:
    """Send a test card to verify the Feishu webhook configuration."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    print(f"Webhook URL: {FEISHU_WEBHOOK_URL or '(not set)'}")
    print(f"Secret: {'set' if FEISHU_WEBHOOK_SECRET else 'not set'}")
    if not FEISHU_WEBHOOK_URL:
        print("FAIL: FEISHU_WEBHOOK_URL is empty")
        return 1
    # Write a real (persistent) test report so the card button never 404s.
    test_path = Path(os.environ.get(
        "HERMES_POSTMORTEM_DIR", "/data/postmortems")) / "feishu-test.md"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(
        "# 🚨 暴涨复盘 — 飞书连接测试\n\n"
        "- **生成时间**：连接测试卡片\n"
        "- **综合评分**：**60.0**（上周期 40.0，跳升 +20.0）\n\n"
        "## 指标明细\n\n"
        "| 指标 | 分数 | 是否触发 | 原因 |\n"
        "|---|---|---|---|\n"
        "| momentumBurst | 18.0 | ✅ 是 | 测试 |\n"
        "| breakout | 12.0 | ✅ 是 | 测试 |\n\n"
        "这是一份用于验证飞书推送与报告链接的测试文件。\n",
        encoding="utf-8")
    ok = send_feishu_card(
        coin="飞书连接测试",
        score=60.0,
        prev_score=40.0,
        fired_names={"momentumBurst", "breakout"},
        report_path=test_path,
        mid=65000.0,
    )
    print("Feishu push:", "OK" if ok else "FAILED (check logs above)")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes surge postmortem")
    parser.add_argument("--simulate", action="store_true",
                        help="Generate a synthetic surge report to verify the pipeline")
    parser.add_argument("--test-feishu", action="store_true",
                        help="Send a test card to verify Feishu webhook push")
    args = parser.parse_args()
    if args.simulate:
        return _simulate()
    if args.test_feishu:
        return _test_feishu()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
