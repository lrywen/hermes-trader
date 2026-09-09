#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SHADOW 模式回测 / 验证脚本
==========================
用途：调整入场参数后，量化验证信号是否真的走通全链路、并进入 shadow_book 纸面账本。

两种模式：
  1) snapshot（默认）：统计某时间点之后的记录，输出信号漏斗 + 拦截分布
     + shadow_book 纸面成交/持仓/胜率统计。适合"改完参数跑一段时间后回看"。
  2) watch：tail -F 实时滚动统计，Ctrl+C 时打印汇总。适合"改完立刻盯着看"。

信号漏斗各层（与交易循环一致）：
  scan 候选 -> 容量/冷却节流(throttle) -> TA 过滤(REJECTED/WEAK) ->
  LLM 研究(verdict PASS/LONG/SHORT) -> runner_entry_gate -> 22 闸门 ->
  shadow_book.OPEN(paper)

数据源（CS-E, 2026-09-08）：
  默认读取结构化的 session-log.jsonl（每行一个 JSON 事件：scan/ta_skip/research/
  execute/shadow_exit）。交易循环与日报都以它为权威漏斗源，字段稳定、不随日志
  排版漂移。旧版靠解析 trading-loop.log 自由文本（"TA" 子串猜测、gate BLOCKED
  要求同行含 "Triggers="），日志格式一变就静默错统——保留为 --log-regex 逃生舱。

  关键分层修正：ta_skip 事件里既有真正的 TA 判定（REJECTED/WEAK），也有容量/
  冷却节流（HELD_THROTTLE/COOLDOWN/RESEARCH_THROTTLE/BLOCKLISTED/SIGNAL_DEDUP/
  JOBS_BACKPRESSURE）。后者发生在 TA 判定**之前**、是省 token / 背压丢弃，
  绝不计入"TA 过滤层"，否则背压丢单会被误读成技术形态不达标。

用法（容器内）：
  python3 scripts/shadow_validate.py                 # snapshot，统计全天（结构化源）
  python3 scripts/shadow_validate.py --since "2026-09-03 01:00"
  python3 scripts/shadow_validate.py --watch
  python3 scripts/shadow_validate.py --log-regex /data/trading-loop.log  # 旧文本路径
"""
import argparse
import json
import os
import re
import time
from collections import Counter
from datetime import datetime

# Audit 2026-09-07 (tail-2 / F5): env-overridable default paths so the script
# works outside the container layout too; CLI flags still win.
DEFAULT_SESSION_LOG = os.environ.get("SESSION_LOG_PATH", "/data/session-log.jsonl")
DEFAULT_LOG = os.environ.get("HERMES_TRADING_LOOP_LOG", "/data/trading-loop.log")

# ---- 结构化路径：TA 判定 vs 容量/冷却节流 ----
# ta_skip.signal 里真正来自 analyze_perception 的技术分析判定只有这两个
# （CONFIRMED/+burst 不发 ta_skip，直接进入 research）。
TA_REJECT_SIGNALS = frozenset({"REJECTED", "WEAK"})
# 发生在 TA 判定之前的节流/背压/去重/黑名单丢弃——单独一层，不污染 TA 计数。
# B-1 的 JOBS_BACKPRESSURE 是作业队列背压丢尾（TA 已通过、尚未付费研究）。
THROTTLE_LABELS = {
    "HELD_THROTTLE": "held_research_throttle",
    "BLOCKLISTED": "coin_blocklist",
    "COOLDOWN": "post_trade_cooldown",
    "RESEARCH_THROTTLE": "re_research_throttle",
    "SIGNAL_DEDUP": "same_setup_content_dedup",
    "JOBS_BACKPRESSURE": "jobs_backpressure_cap",
}


# ============================================================================
# 结构化路径（默认）—— 直接消费 session-log.jsonl，不猜测日志文本
# ============================================================================
class FunnelStats:
    """从结构化 session-log 事件重建信号漏斗。"""

    def __init__(self):
        self.scan_cycles = 0
        self.scan_perceptions = 0          # 所有周期 perceptions 总和
        self.ta = Counter()                 # 仅 REJECTED / WEAK（真正的 TA 判定）
        self.throttle = Counter()           # TA 之前的容量/冷却/去重丢弃
        self.verdict = Counter()            # research.verdict
        # execute 事件（每个方向研判一条）
        self.exec_events = 0
        self.real_filled = 0
        self.runner_block = Counter()       # runner_entry_gate 拦截分类
        self.runner_conf = []               # 被 confidence 地板拦的 conf 值
        self.gate_events = 0                # 穿过 runner、进入 22 闸评估次数
        self.gate_block = Counter()         # 22 闸各闸门命中次数
        self.shadow_would = 0               # shadow 模式下本会成交（paper OPEN）
        self.shadow_closes = []             # (side, coin, pnl_usd)
        self.coins = set()

    # -- runner_entry_gate 自由文本 detail -> 稳定机器分类 -------------------
    # runner gate 的拦截原因目前只编码在 execute.detail（route_verdict 返回的
    # 字符串）里；这里集中一处做前缀分类，日志文案微调只需改这里。
    def classify_runner_detail(self, detail: str) -> None:
        d = detail or ""
        low = d.lower()
        m = re.search(r"confidence\s+([\d.]+)\s*<", d)
        if m:
            self.runner_conf.append(float(m.group(1)))
            if "short" in low:
                self.runner_block["short_confidence_floor"] += 1
            else:
                self.runner_block["confidence_floor"] += 1
        elif "shorts disabled" in low:
            self.runner_block["shorts_disabled"] += 1
        elif "late trend-only chase" in low:
            self.runner_block["late_trend_chase"] += 1
        elif "needs fresh" in low or "whale-only" in low or "fresh breakout" in low:
            self.runner_block["needs_impulse_structure"] += 1
        elif "rsi" in low:
            self.runner_block["rsi_extension"] += 1
        elif "extension" in low:
            self.runner_block["atr_extension"] += 1
        elif "hip-3" in low:
            self.runner_block["hip3_composite_floor"] += 1
        elif "pullback-long shadow" in low:
            self.runner_block["pullback_long_shadow"] += 1
        else:
            self.runner_block["other"] += 1

    @staticmethod
    def _gate_label(name: str) -> str:
        """把 gate_results 的闸门键归到人类可读类别（与 22 闸语义一致）。"""
        n = (name or "").lower()
        if "drawdown" in n:
            return "drawdown_halt"
        if "debate" in n:
            return "debate"
        if "regime" in n or "funding" in n:
            return "market_regime"
        if "liquidity" in n or "volume" in n:
            return "liquidity"
        if "news" in n:
            return "news"
        if "cooldown" in n:
            return "cooldown"
        if "consecutive" in n:
            return "consecutive_loss"
        if "correlation" in n or "correlated" in n:
            return "correlation"
        if "daily" in n:
            return "daily_loss"
        if "concurrent" in n:
            return "max_concurrent"
        if "notional" in n or "cap" in n:
            return "notional_cap"
        return name or "unknown"

    def feed(self, ev: dict) -> None:
        event = ev.get("event")
        coin = ev.get("coin")
        if coin:
            self.coins.add(coin)

        if event == "scan":
            self.scan_cycles += 1
            try:
                self.scan_perceptions += int(ev.get("perceptions")
                                             or ev.get("triggers") or 0)
            except (TypeError, ValueError):
                pass
            for cs in ev.get("coin_scores") or []:
                if isinstance(cs, dict) and cs.get("coin"):
                    self.coins.add(cs["coin"])

        elif event == "ta_skip":
            sig = str(ev.get("signal") or "")
            if sig in TA_REJECT_SIGNALS:
                self.ta[sig] += 1
            else:
                # 容量/冷却/去重/背压丢弃归节流层（含未知新 signal，保守归 other）。
                self.throttle[THROTTLE_LABELS.get(sig, sig.lower() or "other")] += 1

        elif event == "research":
            v = str(ev.get("verdict") or "").upper()
            if v:
                self.verdict[v] += 1

        elif event == "execute":
            self.exec_events += 1
            if ev.get("executed"):
                self.real_filled += 1
                return
            detail = str(ev.get("detail") or "")
            if detail == "shadow_mode_would_execute":
                # shadow 模式唯一的"全链路走通"证据：22 闸全过、已入纸面账本。
                self.shadow_would += 1
                return
            if detail.startswith("runner_gate_blocked"):
                self.classify_runner_detail(detail)
                return
            # 穿过 runner、进入 22 闸：blocked_by / gates 是结构化的。
            blocked_by = ev.get("blocked_by")
            gates = ev.get("gates")
            if isinstance(blocked_by, list) and blocked_by:
                self.gate_events += 1
                for b in blocked_by:
                    bs = str(b).lower()
                    if "volume" in bs or "below floor" in bs:
                        self.gate_block["liquidity"] += 1
                    elif "counter-regime" in bs or "counter-trend" in bs \
                            or "funding" in bs or "chop" in bs:
                        self.gate_block["market_regime"] += 1
                    elif "notional" in bs or "cap" in bs:
                        self.gate_block["notional_cap"] += 1
                    elif "cooldown" in bs:
                        self.gate_block["cooldown"] += 1
                    elif "correlation" in bs or "correlated" in bs:
                        self.gate_block["correlation"] += 1
                    elif "consecutive" in bs:
                        self.gate_block["consecutive_loss"] += 1
                    elif "daily" in bs:
                        self.gate_block["daily_loss"] += 1
                    elif "concurrent" in bs:
                        self.gate_block["max_concurrent"] += 1
                    elif "news" in bs:
                        self.gate_block["news"] += 1
                    else:
                        self.gate_block["other"] += 1
            elif isinstance(gates, dict):
                fails = [k for k, ok in gates.items() if ok is False]
                if fails:
                    self.gate_events += 1
                    for k in fails:
                        self.gate_block[self._gate_label(k)] += 1

        elif event == "shadow_exit":
            try:
                self.shadow_closes.append(
                    (str(ev.get("side") or ""), coin,
                     float(ev.get("realized_pnl_usd") or 0.0))
                )
            except (TypeError, ValueError):
                pass


def _parse_since_ms(since: str) -> int:
    """Parse a local-time 'YYYY-MM-DD HH:MM[:SS]' string to epoch ms.

    Returns 0 when empty/unparseable (no lower bound). Mirrors the old regex
    script's local-time semantics rather than assuming UTC.
    """
    if not since:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(since.strip(), fmt).timestamp() * 1000)
        except ValueError:
            continue
    return 0


def _iter_session_events(path: str):
    """Yield parsed JSON events from the active session log only.

    Rotated ``.gz`` history is intentionally NOT merged here: a rotation can
    land mid-window and silently change counts; for a bounded funnel snapshot
    the active file is the unambiguous source. Use the daily report for spans
    that cross rotations.
    """
    try:
        f = open(path, "r", encoding="utf-8", errors="ignore")
    except OSError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue


def build_funnel(path: str, since_ms: int = 0) -> FunnelStats:
    st = FunnelStats()
    for ev in _iter_session_events(path):
        ts = ev.get("ts")
        if since_ms and isinstance(ts, (int, float)) and ts < since_ms:
            continue
        st.feed(ev)
    return st


def report_funnel(st: FunnelStats, since: str, tail_label: str = ""):
    line = "=" * 72
    print("\n" + line)
    print(f"SHADOW 验证报告（结构化事件源）  {tail_label}  (since {since})")
    print(line)

    total_dir = st.verdict["LONG"] + st.verdict["SHORT"]
    print("\n■ 信号漏斗")
    print(f"  scan 周期       : {st.scan_cycles}   候选 perceptions 累计={st.scan_perceptions}")
    if st.throttle:
        print("  容量/冷却节流    : (TA 判定之前的丢弃，非技术形态拒绝)")
        for k, n in st.throttle.most_common():
            print(f"    {k:<28}: {n}")
    if st.ta:
        print(f"  TA 过滤         : REJECTED={st.ta['REJECTED']}  WEAK={st.ta['WEAK']}")
    print(f"  LLM 研判 verdict : PASS={st.verdict['PASS']}  "
          f"LONG={st.verdict['LONG']}  SHORT={st.verdict['SHORT']}  "
          f"CLOSE={st.verdict['CLOSE']}")
    print(f"  方向信号合计     : {total_dir}  (LONG+SHORT，进入下单评估)")

    print("\n■ runner_entry_gate 拦截（22 闸之前的总阀门）")
    if st.runner_block:
        for k, n in st.runner_block.most_common():
            print(f"    {k:<26}: {n}")
        if st.runner_conf:
            c = Counter(st.runner_conf)
            dist = "  ".join(f"{k:.2f}×{v}" for k, v in sorted(c.items()))
            print(f"    被 confidence 地板拦的 conf 分布: {dist}")
    else:
        print("    （无 runner 拦截）")

    print("\n■ 22 闸门拦截分布（一次评估可命中多闸）")
    print(f"  进入 22 闸评估次数: {st.gate_events}")
    if st.gate_block:
        for k, n in st.gate_block.most_common():
            print(f"    {k:<20}: {n}")
    else:
        print("    （无 22 闸拦截记录）")

    print("\n■ shadow_book 纸面成交（全链路走通的最终证据）")
    print(f"  OPEN (paper, would_execute): {st.shadow_would}")
    print(f"  实盘成交(executed=true)    : {st.real_filled}   "
          f"[shadow 模式应为 0]")
    print(f"  CLOSE(paper) 笔数          : {len(st.shadow_closes)}")
    if st.shadow_closes:
        pnls = [p for _, _, p in st.shadow_closes]
        wins = sum(1 for p in pnls if p > 0)
        total = sum(pnls)
        print(f"    胜率: {wins}/{len(pnls)} = {wins/len(pnls)*100:.0f}%  "
              f"累计纸面盈亏=${total:.2f}")

    print("\n■ 结论判定")
    if st.shadow_would > 0:
        print(f"  ✅ 参数调整生效：已有 {st.shadow_would} 笔信号走通全链路进入纸面账本。")
    elif total_dir > 0:
        print("  ⚠️ 有方向信号但 0 笔纸面成交 -> 仍被拦截，请看上方拦截分布定位。")
    else:
        print("  ℹ️ 时间窗内暂无方向信号（LONG/SHORT=0），可能市场无机会或观察窗太短。")
    print(line + "\n")


def run_snapshot_events(path: str, since: str):
    st = build_funnel(path, _parse_since_ms(since))
    print(f"(扫描结构化事件 {path})")
    report_funnel(st, since or "日志起点")


def run_watch_events(path: str):
    st = FunnelStats()
    print(f"[watch] tail -F {path} (structured) ... Ctrl+C 输出汇总")
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(1.0)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                st.feed(ev)
                if ev.get("event") == "execute" and \
                        str(ev.get("detail") or "") == "shadow_mode_would_execute":
                    print(f"  >> 纸面成交: {ev.get('coin')} "
                          f"size=${ev.get('size_usd')}")
    except KeyboardInterrupt:
        report_funnel(st, "watch 起点", tail_label="[实时]")


# ============================================================================
# 旧文本正则路径（--log-regex 逃生舱）—— 保留原行为，日志漂移时可能错统
# ============================================================================
RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
RE_VERDICT = re.compile(r"verdict=(PASS|LONG|SHORT|CLOSE)")
RE_TA = re.compile(r"\b(REJECTED|WEAK|CONFIRMED)\b")
RE_RUNNER = re.compile(r"\[runner_gate\]\s+(\S+)\s+BLOCKED:\s*(.*)")
RE_GATE_BLOCK = re.compile(r"🚫\s+(\S+)\s+BLOCKED\s+—(.*)")
RE_SHADOW_OPEN = re.compile(r"\[shadow_book\]\s+OPEN\s+(long|short)\s+(\S+).*notional=\$([\d.]+).*lev=(\d+)")
RE_SHADOW_CLOSE = re.compile(r"\[shadow_book\]\s+CLOSE\s+(long|short)\s+(\S+).*pnl=?\$?(-?[\d.]+)")
RE_LLM_OPEN = re.compile(r"LLM circuit OPEN")
RE_LLM_TRIP = re.compile(r"circuit (?:is )?OPEN for|breaker tripped|OPEN for")
RE_LLM_OK = re.compile(r"OpenRouter-OK|openrouter.*200|chat/completions\" 200")


class Stats:
    def __init__(self):
        self.verdict = Counter()
        self.ta = Counter()
        self.runner_block = Counter()
        self.runner_conf = []
        self.gate_events = 0
        self.gate_block = Counter()
        self.shadow_opens = []
        self.shadow_closes = []
        self.llm_open = 0
        self.llm_trip = 0
        self.llm_ok = 0

    def classify_runner(self, detail):
        if "confidence" in detail and "<" in detail:
            m = re.search(r"confidence\s+([\d.]+)\s*<", detail)
            if m:
                self.runner_conf.append(float(m.group(1)))
            self.runner_block["confidence_floor"] += 1
        elif "trend-only chase" in detail:
            self.runner_block["late_trend_chase"] += 1
        elif "fresh impulse" in detail or "structure" in detail:
            self.runner_block["needs_impulse_structure"] += 1
        elif "rsi" in detail.lower() or "extension" in detail.lower() or "overbought" in detail.lower():
            self.runner_block["rsi_extension"] += 1
        elif "short" in detail.lower():
            self.runner_block["short_rule"] += 1
        else:
            self.runner_block["other"] += 1

    def classify_gates(self, reasons):
        self.gate_events += 1
        if "drawdown halt" in reasons:
            self.gate_block["drawdown(已修复)"] += 1
        if "multi-agent debate blocked" in reasons or "analysts agree" in reasons:
            self.gate_block["debate"] += 1
        if "counter-regime" in reasons or "counter-trend" in reasons or "chop" in reasons or "against funding" in reasons:
            self.gate_block["market_regime"] += 1
        if "below floor" in reasons or "volume" in reasons:
            self.gate_block["liquidity"] += 1
        if "news" in reasons and "news_risk=True" in reasons:
            self.gate_block["news"] += 1
        if "cooldown" in reasons:
            self.gate_block["cooldown"] += 1
        if "consecutive" in reasons:
            self.gate_block["consecutive_loss"] += 1
        if "correlation" in reasons or "correlated" in reasons:
            self.gate_block["correlation"] += 1
        if "daily loss" in reasons or "daily_loss" in reasons:
            self.gate_block["daily_loss"] += 1
        if "max concurrent" in reasons or "concurrent" in reasons:
            self.gate_block["max_concurrent"] += 1

    def feed(self, line):
        m = RE_VERDICT.search(line)
        if m:
            self.verdict[m.group(1)] += 1
        # TA verdict（只统计明确的 TA 判定行，避免误伤）
        if "TA" in line or "ta_" in line:
            mt = RE_TA.search(line)
            if mt and ("verdict" in line.lower() or "TA" in line):
                self.ta[mt.group(1)] += 1
        m = RE_RUNNER.search(line)
        if m:
            self.classify_runner(m.group(2))
            return
        m = RE_GATE_BLOCK.search(line)
        if m and "runner_gate" not in line and "Triggers=" in line:
            self.classify_gates(m.group(2))
        m = RE_SHADOW_OPEN.search(line)
        if m:
            self.shadow_opens.append((m.group(1), m.group(2), float(m.group(3)), int(m.group(4))))
        m = RE_SHADOW_CLOSE.search(line)
        if m:
            try:
                self.shadow_closes.append((m.group(1), m.group(2), float(m.group(3))))
            except ValueError:
                pass
        if RE_LLM_OPEN.search(line):
            self.llm_open += 1
        if RE_LLM_TRIP.search(line):
            self.llm_trip += 1
        if RE_LLM_OK.search(line):
            self.llm_ok += 1


def report(st: Stats, since: str, tail_label: str = ""):
    line = "=" * 72
    print("\n" + line)
    print(f"SHADOW 验证报告（旧文本正则源 --log-regex）  {tail_label}  (since {since})")
    print(line)

    print("\n■ 信号漏斗")
    v = st.verdict
    total_dir = v["LONG"] + v["SHORT"]
    print(f"  verdict 路由   : PASS={v['PASS']}  LONG={v['LONG']}  SHORT={v['SHORT']}  CLOSE={v['CLOSE']}")
    print(f"  方向信号合计    : {total_dir}  (LONG+SHORT，进入下单评估)")
    if st.ta:
        print(f"  TA 过滤        : CONFIRMED={st.ta['CONFIRMED']}  WEAK={st.ta['WEAK']}  REJECTED={st.ta['REJECTED']}")

    print("\n■ LLM 研究")
    print(f"  成功(200/OK)={st.llm_ok}  熔断短路={st.llm_open}  跳闸(trip)={st.llm_trip}")

    print("\n■ runner_entry_gate 拦截（22 闸之前的总阀门）")
    if st.runner_block:
        for k, n in st.runner_block.most_common():
            print(f"    {k:<24}: {n}")
        if st.runner_conf:
            c = Counter(st.runner_conf)
            dist = "  ".join(f"{k:.2f}×{v}" for k, v in sorted(c.items()))
            print(f"    被 confidence 地板拦的 conf 分布: {dist}")
            print(f"    -> 若当前阈值 0.62，则其中 conf>=0.62 的有 "
                  f"{sum(1 for x in st.runner_conf if x >= 0.62)} 个本应放行")
    else:
        print("    （无 runner 拦截）")

    print("\n■ 22 闸门拦截分布（一次评估可命中多闸）")
    print(f"  进入 22 闸评估次数: {st.gate_events}")
    if st.gate_block:
        for k, n in st.gate_block.most_common():
            print(f"    {k:<20}: {n}")
    else:
        print("    （无 22 闸拦截记录）")

    print("\n■ shadow_book 纸面成交（全链路走通的最终证据）")
    print(f"  OPEN (paper) 笔数 : {len(st.shadow_opens)}")
    if st.shadow_opens:
        side_c = Counter(s for s, _, _, _ in st.shadow_opens)
        coins = Counter(c for _, c, _, _ in st.shadow_opens)
        notional = sum(n for _, _, n, _ in st.shadow_opens)
        print(f"    方向: long={side_c['long']} short={side_c['short']}  "
              f"累计名义本金=${notional:.2f}")
        print(f"    涉及币种: {dict(coins.most_common(10))}")
    print(f"  CLOSE(paper) 笔数 : {len(st.shadow_closes)}")
    if st.shadow_closes:
        pnls = [p for _, _, p in st.shadow_closes]
        wins = sum(1 for p in pnls if p > 0)
        total = sum(pnls)
        print(f"    胜率: {wins}/{len(pnls)} = {wins/len(pnls)*100:.0f}%  "
              f"累计纸面盈亏=${total:.2f}")

    print("\n■ 结论判定")
    if len(st.shadow_opens) > 0:
        print(f"  ✅ 参数调整生效：已有 {len(st.shadow_opens)} 笔信号走通全链路进入纸面账本。")
    elif total_dir > 0:
        print("  ⚠️ 有方向信号但 0 笔纸面成交 -> 仍被闸门拦截，请看上方拦截分布定位。")
    else:
        print("  ℹ️ 时间窗内暂无方向信号（LONG/SHORT=0），可能市场无机会或观察窗太短。")
    print(line + "\n")


def ts_of(line):
    m = RE_TS.match(line)
    return m.group(1) if m else ""


def run_snapshot(log_path, since):
    st = Stats()
    matched = 0
    with open(log_path, "r", errors="ignore") as f:
        for line in f:
            ts = ts_of(line)
            if since and ts and ts < since:
                continue
            if since and not ts:
                continue
            matched += 1
            st.feed(line)
    print(f"(扫描 {log_path}，命中 {matched} 行)")
    report(st, since or "日志起点")


def run_watch(log_path):
    st = Stats()
    print(f"[watch] tail -F {log_path} ... Ctrl+C 输出汇总")
    try:
        with open(log_path, "r", errors="ignore") as f:
            f.seek(0, 2)  # 跳到末尾，只看新增
            while True:
                line = f.readline()
                if not line:
                    time.sleep(1.0)
                    continue
                st.feed(line)
                if RE_SHADOW_OPEN.search(line):
                    print("  >> 纸面成交: " + line.strip()[-160:])
    except KeyboardInterrupt:
        report(st, "watch 起点", tail_label="[实时]")


def main():
    ap = argparse.ArgumentParser(description="SHADOW 模式回测/验证")
    ap.add_argument("--session-log", default=DEFAULT_SESSION_LOG,
                    help="结构化事件 JSONL 路径（默认 SESSION_LOG_PATH）")
    ap.add_argument("--log-regex", default="", metavar="LOG_PATH",
                    help="改用旧 trading-loop.log 自由文本正则路径（逃生舱，默认关闭）")
    ap.add_argument("--log", default="", help="（--log-regex 的旧别名）")
    ap.add_argument("--since", default="", help='起始时间，如 "2026-09-03 01:00:00"')
    ap.add_argument("--watch", action="store_true", help="实时 tail 模式")
    args = ap.parse_args()

    regex_log = args.log_regex or args.log
    if regex_log:
        log_path = regex_log if regex_log != "default" else DEFAULT_LOG
        if args.watch:
            run_watch(log_path)
        else:
            run_snapshot(log_path, args.since)
    else:
        if args.watch:
            run_watch_events(args.session_log)
        else:
            run_snapshot_events(args.session_log, args.since)


if __name__ == "__main__":
    main()
